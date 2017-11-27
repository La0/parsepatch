# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from libmozdata.hgmozilla import RawRevision
import re
import requests
import six
from .logger import logger
from . import utils


NUMS_PAT = re.compile(r'^@@ -([0-9]+),?([0-9]+)? \+([0-9]+),?([0-9]+)? @@')
FIRST = {' ', '+', '-'}
EMPTY_PAT = re.compile(r'^[+-][ \t]*(?://.*)?(?:/\*[^\*]*[\*]+/)?[ \t]*$')


class Patch(object):
    """The goal of Patch is to be able to parse a patch coming
       from a stream to limit memory use.
       The parse method return a dictionary containing the
       interesting files (.cpp, .h, ...) and the modified lines.
       By the way, the 'empty' lines (whites or comments) are removed.
    """

    def __init__(self, lines_gen):
        self.index = 0
        self.lines = []
        self.N = 0
        self.get_lines = self._lines(lines_gen)
        self.conditions = []
        self.added = None
        self.deleted = None
        self.results = {}
        self.filename = ''
        self.changeset = ''

    @staticmethod
    def parse_changeset(chgset, channel='nightly', chunk_size=1000000):

        def lines_chunk(it):
            last = None
            for chunk in it:
                chunk = chunk.split('\n')
                if last is not None:
                    chunk[0] = last + chunk[0]
                last = chunk.pop()
                yield chunk

        logger.info('Get patch for revision {}'.format(chgset))
        url = '{}/{}'.format(RawRevision.get_url(channel),
                             chgset)
        r = requests.get(url, stream=True)
        it = r.iter_content(chunk_size=chunk_size,
                            decode_unicode=True)
        p = Patch(lines_chunk(it))
        p.changeset = chgset
        return p.parse()

    @staticmethod
    def parse_patch(patch):
        if isinstance(six.strings, patch):
            patch = patch.split('\n')

        def gen(x):
            yield x

        p = Patch(gen(patch))
        return p.parse()

    @staticmethod
    def parse_file(filename):
        with open(filename, 'r') as In:
            patch = In.read()
            return Patch.parse_patch(patch)

    def neighbourhood(self, index):
        print('----------------------------------')
        print('INDEX = ' + str(index))
        for i in range(index - 5, index + 6):
            if i == index:
                print('~~~~~~~~~~~~~~~~~~~')
                print(self.lines[i])
                print('~~~~~~~~~~~~~~~~~~~')
            else:
                print(self.lines[i])
        print('----------------------------------')

    def _get_lines(self, lines_gen):
        for lines in lines_gen:
            self.N = len(lines)
            if self.index < self.N:
                self.lines = lines
                n = (yield)
                if n is not None:
                    self.index = n
            else:
                self.index -= self.N

    def _lines(self, lines_gen):
        gen = self._get_lines(lines_gen)
        for _ in gen:
            while self.index < self.N:
                line = self.lines[self.index]
                if self._check(line):
                    n = (yield line)
                    if n is None:
                        n = 1
                    diff = self.N - (self.index + n)
                    if diff <= 0:
                        try:
                            # no more data
                            gen.send(-diff)
                        except StopIteration:
                            # raise StopIteration
                            return
                    else:
                        self.index += n
                else:
                    if self.conditions:
                        self.conditions.pop()
                    yield None

    def _check(self, line):
        if self.conditions:
            return self.conditions[-1](line)
        return True

    def _condition(self, checker):
        self.conditions.append(checker)

    def line(self):
        return self.lines[self.index]

    def move(self, n=1):
        self.get_lines.send(n)

    def first(self):
        line = self.line()
        return line[0] if line else ''

    def parse_numbers(self, line=None):
        if not line:
            line = self.line()
        m = NUMS_PAT.search(line)
        n = list(map(lambda x: int(x) if x else 1,
                     m.groups()))
        return n[:2], n[2:]

    def skip_binary(self):
        self._condition(lambda x: bool(x))
        for line in self.get_lines:
            if line is None:
                break

    def is_binary(self):
        return self.line() == 'GIT binary patch'

    def skip_deleted_file(self):
        self.skip_useless()
        if self.is_binary():
            self.skip_binary()
        elif self.line().startswith('@'):
            minus, _ = self.parse_numbers()
            self.move(minus[1])

    def skip_new_file(self):
        self.skip_useless()
        if self.is_binary():
            self.skip_binary()
        elif self.line().startswith('@'):
            _, plus = self.parse_numbers()
            self.move(plus[1])
        if self.filename:
            self.results[self.filename] = {'new': True}

    def next_diff(self):
        self._condition(lambda x: not x.startswith('diff --git a/'))
        for line in self.get_lines:
            if line is None:
                return True
        return False

    def get_files(self):
        line = self.line()
        toks = line.split(' ')
        old_p = toks[2]
        old_p = old_p[2:] if old_p.startswith('a/') else old_p
        new_p = toks[3]
        new_p = new_p[2:] if new_p.startswith('b/') else new_p
        self.added = []
        self.deleted = []
        if utils.is_interesting_file(new_p):
            self.filename = new_p
            return True
        else:
            self.filename = ''
        return False

    def skip_useless(self):
        self._condition(lambda x: x.startswith('---') or
                        x.startswith('+++') or
                        x.startswith('index ') or
                        x.startswith('old mode') or
                        x.startswith('new mode'))
        for line in self.get_lines:
            if line is None:
                break

    def get_signed_count(self, line, count):
        return -count if EMPTY_PAT.match(line) else count

    def count_minus(self, count):
        self._condition(lambda x: x.startswith('-'))
        for line in self.get_lines:
            if line is None:
                break
            scount = self.get_signed_count(line, count)
            self.deleted.append(scount)
            count += 1
            
    def parse_hunk(self, line):

        def check(x):
            if x:
                return x[0] in FIRST
            return False

        _, plus = self.parse_numbers(line)
        count = plus[0]
        self._condition(check)
        for line in self.get_lines:
            if line is None:
                break
            first = line[0]
            if first == ' ':
                count += 1
            elif first == '+':
                scount = self.get_signed_count(line, count)
                self.added.append(scount)
                count += 1
            elif first == '-':
                # here we get the line number where the deleted lines
                # should be in the new file
                scount = self.get_signed_count(line, count)
                self.deleted.append(scount)
                self.count_minus(count + 1)

    def parse_hunks(self, line):
        self._condition(lambda x: x.startswith('@'))
        for line in self.get_lines:
            if line is None:
                break
            self.parse_hunk(line)

    @staticmethod
    def get_touched(added, deleted):
        # negative line numbers are for empty lines (i.e. whites, comments, ...)
        # off course we keep positive lines
        # but we keep common lines which aren't both negative
        # if added=[1,2,3,4,-5,-6,-7,8,10] and deleted=[4,5,6,-7,9,-10]
        # then res_a=[1,2,3,8], res_d=[9], res_t=[4,5,6,10]
        added = set(added)
        deleted = set(deleted)

        touched = set(abs(x) for x in added
                      if (x > 0 and {x, -x} & deleted))
        touched |= set(abs(x) for x in deleted
                       if (x > 0 and {x, -x} & added))
        added = [x for x in added if x > 0 and x not in touched]
        deleted = [x for x in deleted if x > 0 and x not in touched]
        touched = list(sorted(touched))
        added = list(sorted(added))
        deleted = list(sorted(deleted))

        return added, deleted, touched
            
    def get_changes(self):
        for line in self.get_lines:
            if line.startswith('new file'):
                self.skip_new_file()
                break
            elif line.startswith('deleted file'):
                self.skip_deleted_file()
                break
            else:
                self.skip_useless()
                line = self.line()
                if line.startswith('@'):
                    self.parse_hunks(line)
                break

        if self.added or self.deleted:
            added, deleted, touched = Patch.get_touched(self.added,
                                                        self.deleted)
            self.results[self.filename] = {'added': added,
                                           'deleted': deleted,
                                           'touched': touched,
                                           'new': False}

    def parse(self):
        try:
            while self.next_diff():
                if self.get_files():
                    self.move()
                    self.get_changes()
                else:
                    self.move()
        except StopIteration:
            pass
        except Exception:
            e = 'Error in parsing patch with revision {}'
            logger.error(e.format(self.chgset))
            return {}
        return self.results