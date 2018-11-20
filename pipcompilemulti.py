#!/usr/bin/env python
"""
Build locked requirements files for each of:
    base.in
    test.in
    local.in

External dependencies are hard-pinned using ==
Internal dependencies are soft-pinned using ~=
".post23423" version postfixes are truncated
"""

import os
import re
import glob
import hashlib
import logging
import itertools
import subprocess
from fnmatch import fnmatch

import click
from toposort import toposort_flatten


__author__ = 'Peter Demin'
__email__ = 'peterdemin@gmail.com'
__version__ = '1.2.2'


logger = logging.getLogger("pip-compile-multi")

DEFAULT_HEADER = """
#
# This file is autogenerated by pip-compile-multi
# To update, run:
#
#    pip-compile-multi
#
""".lstrip()


OPTIONS = {
    'compatible_patterns': [],
    'base_dir': 'requirements',
    'forbid_post': [],
    'in_ext': 'in',
    'out_ext': 'txt',
    'header_file': None,
}


@click.group(invoke_without_command=True)
@click.pass_context
@click.option('--compatible', '-c', multiple=True,
              help='Glob expression for packages with compatible (~=) '
                   'version constraint. Can be supplied multiple times.')
@click.option('--forbid-post', '-P', multiple=True,
              help="Environment name (base, test, etc) that cannot have "
                   'packages with post-release versions (1.2.3.post777). '
                   'Can be supplied multiple times.')
@click.option('--generate-hashes', '-g', multiple=True,
              help='Environment name (base, test, etc) that needs '
                   'packages hashes. '
                   'Can be supplied multiple times.')
@click.option('--directory', '-d', default=OPTIONS['base_dir'],
              help='Directory path with requirements files.')
@click.option('--in-ext', '-i', default=OPTIONS['in_ext'],
              help='File extension of input files.')
@click.option('--out-ext', '-o', default=OPTIONS['out_ext'],
              help='File extension of output files.')
@click.option('--header', '-h', default='',
              help='File path with custom header text for generated files.')
@click.option('--only-name', '-n', multiple=True,
              help='Compile only for passed environment names and their '
                   'references. Can be supplied multiple times.')
@click.option('--upgrade/--no-upgrade', default=True,
              help='Upgrade package version (default true)')
def cli(ctx, compatible, forbid_post, generate_hashes, directory,
        in_ext, out_ext, header, only_name, upgrade):
    """Recompile"""
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    OPTIONS.update({
        'compatible_patterns': compatible,
        'forbid_post': set(forbid_post),
        'add_hashes': set(generate_hashes),
        'base_dir': directory,
        'in_ext': in_ext,
        'out_ext': out_ext,
        'header_file': header or None,
        'include_names': only_name,
        'upgrade': upgrade,
    })
    if ctx.invoked_subcommand is None:
        recompile()


def recompile():
    """
    Compile requirements files for all environments.
    """
    pinned_packages = {}
    env_confs = discover(
        os.path.join(
            OPTIONS['base_dir'],
            '*.' + OPTIONS['in_ext'],
        ),
    )
    if OPTIONS['header_file']:
        with open(OPTIONS['header_file']) as fp:
            base_header_text = fp.read()
    else:
        base_header_text = DEFAULT_HEADER
    hashed_by_reference = set()
    for name in OPTIONS['add_hashes']:
        hashed_by_reference.update(
            reference_cluster(env_confs, name)
        )
    included_and_refs = set(OPTIONS['include_names'])
    for name in set(included_and_refs):
        included_and_refs.update(
            recursive_refs(env_confs, name)
        )
    for conf in env_confs:
        if included_and_refs:
            if conf['name'] not in included_and_refs:
                # Skip envs that are not included or referenced by included:
                continue
        rrefs = recursive_refs(env_confs, conf['name'])
        add_hashes = conf['name'] in hashed_by_reference
        env = Environment(
            name=conf['name'],
            ignore=merged_packages(pinned_packages, rrefs),
            forbid_post=conf['name'] in OPTIONS['forbid_post'],
            add_hashes=add_hashes,
        )
        logger.info("Locking %s to %s. References: %r",
                    env.infile, env.outfile, sorted(rrefs))
        env.create_lockfile()
        header_text = generate_hash_comment(env.infile) + base_header_text
        env.replace_header(header_text)
        env.add_references(conf['refs'])
        pinned_packages[conf['name']] = env.packages


def merged_packages(env_packages, names):
    """
    Return union set of environment packages with given names

    >>> sorted(merged_packages(
    ...     {
    ...         'a': {'x': 1, 'y': 2},
    ...         'b': {'y': 2, 'z': 3},
    ...         'c': {'z': 3, 'w': 4}
    ...     },
    ...     ['a', 'b']
    ... ).items())
    [('x', 1), ('y', 2), ('z', 3)]
    """
    combined_packages = sorted(itertools.chain.from_iterable(
        env_packages[name].items()
        for name in names
    ))
    result = {}
    errors = set()
    for name, version in combined_packages:
        if name in result:
            if result[name] != version:
                errors.add((name, version, result[name]))
        else:
            result[name] = version
    if errors:
        for error in sorted(errors):
            logger.error(
                "Package %s was resolved to different "
                "versions in different environments: %s and %s",
                error[0], error[1], error[2],
            )
        raise RuntimeError(
            "Please add constraints for the package version listed above"
        )
    return result


def recursive_refs(envs, name):
    """
    Return set of recursive refs for given env name

    >>> local_refs = sorted(recursive_refs([
    ...     {'name': 'base', 'refs': []},
    ...     {'name': 'test', 'refs': ['base']},
    ...     {'name': 'local', 'refs': ['test']},
    ... ], 'local'))
    >>> local_refs == ['base', 'test']
    True
    """
    refs_by_name = {
        env['name']: set(env['refs'])
        for env in envs
    }
    refs = refs_by_name[name]
    if refs:
        indirect_refs = set(itertools.chain.from_iterable([
            recursive_refs(envs, ref)
            for ref in refs
        ]))
    else:
        indirect_refs = set()
    return set.union(refs, indirect_refs)


def reference_cluster(envs, name):
    """
    Return set of all env names referencing or
    referenced by given name.

    >>> cluster = sorted(reference_cluster([
    ...     {'name': 'base', 'refs': []},
    ...     {'name': 'test', 'refs': ['base']},
    ...     {'name': 'local', 'refs': ['test']},
    ... ], 'test'))
    >>> cluster == ['base', 'local', 'test']
    True
    """
    edges = [
        set([env['name'], ref])
        for env in envs
        for ref in env['refs']
    ]
    prev, cluster = set(), set([name])
    while prev != cluster:
        # While cluster grows
        prev = set(cluster)
        to_visit = []
        for edge in edges:
            if cluster & edge:
                # Add adjacent nodes:
                cluster |= edge
            else:
                # Leave only edges that are out
                # of cluster for the next round:
                to_visit.append(edge)
        edges = to_visit
    return cluster


class Environment(object):
    """requirements file"""

    RE_REF = re.compile(r'^(?:-r|--requirement)\s*(?P<path>\S+).*$')

    def __init__(self, name, ignore=None, forbid_post=False, add_hashes=False):
        """
        name - name of the environment, e.g. base, test
        ignore - set of package names to omit in output
        """
        self.name = name
        self.ignore = ignore or {}
        self.forbid_post = forbid_post
        self.add_hashes = add_hashes
        self.packages = {}

    def create_lockfile(self):
        """
        Write recursive dependencies list to outfile
        with hard-pinned versions.
        Then fix it.
        """
        process = subprocess.Popen(
            self.pin_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate()
        if process.returncode == 0:
            self.fix_lockfile()
        else:
            logger.critical("ERROR executing %s", ' '.join(self.pin_command))
            logger.critical("Exit code: %s", process.returncode)
            logger.critical(stdout.decode('utf-8'))
            logger.critical(stderr.decode('utf-8'))
            raise RuntimeError("Failed to pip-compile {0}".format(self.infile))

    @classmethod
    def parse_references(cls, filename):
        """
        Read filename line by line searching for pattern:

        -r file.in
        or
        --requirement file.in

        return set of matched file names without extension.
        E.g. ['file']
        """
        references = set()
        for line in open(filename):
            matched = cls.RE_REF.match(line)
            if matched:
                reference = matched.group('path')
                reference_base = os.path.splitext(reference)[0]
                references.add(reference_base)
        return references

    @property
    def infile(self):
        """Path of the input file"""
        return os.path.join(OPTIONS['base_dir'],
                            '{0}.{1}'.format(self.name, OPTIONS['in_ext']))

    @property
    def outfile(self):
        """Path of the output file"""
        return os.path.join(OPTIONS['base_dir'],
                            '{0}.{1}'.format(self.name, OPTIONS['out_ext']))

    @property
    def pin_command(self):
        """Compose pip-compile shell command"""
        parts = [
            'pip-compile',
            '--no-header',
            '--verbose',
            '--rebuild',
            '--no-index',
            '--output-file', self.outfile,
            self.infile,
        ]
        if OPTIONS['upgrade']:
            parts.insert(3, '--upgrade')
        if self.add_hashes:
            parts.insert(1, '--generate-hashes')
        return parts

    def fix_lockfile(self):
        """Run each line of outfile through fix_pin"""
        with open(self.outfile, 'rt') as fp:
            lines = [
                self.fix_pin(line)
                for line in self.concatenated(fp)
            ]
        with open(self.outfile, 'wt') as fp:
            fp.writelines([
                line + '\n'
                for line in lines
                if line is not None
            ])

    @staticmethod
    def concatenated(fp):
        """Read lines from fp concatenating on backslash (\\)"""
        line_parts = []
        for line in fp:
            line = line.strip()
            if line.endswith('\\'):
                line_parts.append(line[:-1].rstrip())
            else:
                line_parts.append(line)
                yield ' '.join(line_parts)
                line_parts[:] = []
        if line_parts:
            # Impossible:
            raise RuntimeError("Compiled file ends with backslash \\")

    def fix_pin(self, line):
        """
        Fix dependency by removing post-releases from versions
        and loosing constraints on internal packages.
        Drop packages from ignore set

        Also populate packages set
        """
        dep = Dependency(line)
        if dep.valid:
            if dep.package in self.ignore:
                ignored_version = self.ignore[dep.package]
                if ignored_version is not None:
                    # ignored_version can be None to disable conflict detection:
                    if dep.version and dep.version != ignored_version:
                        logger.error(
                            "Package %s was resolved to different "
                            "versions in different environments: %s and %s",
                            dep.package, dep.version, ignored_version,
                        )
                        raise RuntimeError(
                            "Please add constraints for the package "
                            "version listed above"
                        )
                return None
            self.packages[dep.package] = dep.version
            if self.forbid_post or dep.is_compatible:
                # Always drop post for internal packages
                dep.drop_post()
            return dep.serialize()
        return line.strip()

    def add_references(self, other_names):
        """Add references to other_names in outfile"""
        if not other_names:
            # Skip on empty list
            return
        with open(self.outfile, 'rt') as fp:
            header, body = self.split_header(fp)
        with open(self.outfile, 'wt') as fp:
            fp.writelines(header)
            fp.writelines(
                '-r {0}.{1}\n'.format(other_name, OPTIONS['out_ext'])
                for other_name in sorted(other_names)
            )
            fp.writelines(body)

    @staticmethod
    def split_header(fp):
        """
        Read file pointer and return pair of lines lists:
        first - header, second - the rest.
        """
        body_start, header_ended = 0, False
        lines = []
        for line in fp:
            if line.startswith('#') and not header_ended:
                # Header text
                body_start += 1
            else:
                header_ended = True
            lines.append(line)
        return lines[:body_start], lines[body_start:]

    def replace_header(self, header_text):
        """Replace pip-compile header with custom text"""
        with open(self.outfile, 'rt') as fp:
            _, body = self.split_header(fp)
        with open(self.outfile, 'wt') as fp:
            fp.write(header_text)
            fp.writelines(body)


class Dependency(object):
    """Single dependency line"""

    COMMENT_JUSTIFICATION = 26

    # Example:
    # unidecode==0.4.21         # via myapp
    # [package]  [version]      [comment]
    RE_DEPENDENCY = re.compile(
        r'(?iu)(?P<package>\S+)'
        r'=='
        r'(?P<version>\S+)'
        r'\s*'
        r'(?P<hashes>(?:--hash=\S+\s*)+)?'
        r'(?P<comment>#.*)?$'
    )
    RE_EDITABLE_FLAG = re.compile(
        r'^-e '
    )
    # -e git+https://github.com/ansible/docutils.git@master#egg=docutils
    # -e "git+https://github.com/zulip/python-zulip-api.git@
    #                 0.4.1#egg=zulip==0.4.1_git&subdirectory=zulip"
    RE_VCS_DEPENDENCY = re.compile(
        r'(?iu)(?P<editable>-e)?'
        r'\s*'
        r'(?P<prefix>\S+#egg=)'
        r'(?P<package>[a-z0-9-_.]+)'
        r'(?P<postfix>\S+)'
        r'\s*'
        r'(?P<comment>#.*)?$'
    )

    def __init__(self, line):
        regular = self.RE_DEPENDENCY.match(line)
        if regular:
            self.valid = True
            self.is_vcs = False
            self.package = regular.group('package')
            self.version = regular.group('version').strip()
            self.hashes = (regular.group('hashes') or '').strip()
            self.comment = (regular.group('comment') or '').strip()
            return
        vcs = self.RE_VCS_DEPENDENCY.match(line)
        if vcs:
            self.valid = True
            self.is_vcs = True
            self.package = vcs.group('package')
            self.version = ''
            self.hashes = ''  # No way!
            self.comment = (vcs.group('comment') or '').strip()
            self.line = line
            return
        self.valid = False

    def serialize(self):
        """
        Render dependency back in string using:
            ~= if package is internal
            == otherwise
        """
        if self.is_vcs:
            return self.without_editable(self.line).strip()
        equal = '~=' if self.is_compatible else '=='
        package_version = '{package}{equal}{version}  '.format(
            package=self.without_editable(self.package),
            version=self.version,
            equal=equal,
        )
        if self.hashes:
            hashes = self.hashes.split()
            lines = [package_version.strip()]
            lines.extend(hashes)
            if self.comment:
                lines.append(self.comment)
            return ' \\\n    '.join(lines)
        else:
            return '{0}{1}'.format(
                package_version.ljust(self.COMMENT_JUSTIFICATION),
                self.comment,
            ).rstrip()  # rstrip for empty comment

    @classmethod
    def without_editable(cls, line):
        """
        Remove the editable flag.
        It's there because pip-compile can't yet do without it
        (see https://github.com/jazzband/pip-tools/issues/272 upstream),
        but in the output of pip-compile it's no longer needed.
        """
        if 'git+git@' in line:
            # git+git can't be installed without -e:
            return line
        return cls.RE_EDITABLE_FLAG.sub('', line)

    @property
    def is_compatible(self):
        """Check if package name is matched by compatible_patterns"""
        for pattern in OPTIONS['compatible_patterns']:
            if fnmatch(self.package.lower(), pattern):
                return True
        return False

    def drop_post(self):
        """Remove .postXXXX postfix from version"""
        post_index = self.version.find('.post')
        if post_index >= 0:
            self.version = self.version[:post_index]


def discover(glob_pattern):
    """
    Find all files matching given glob_pattern,
    parse them, and return list of environments:

    >>> envs = discover("requirements/*.in")
    >>> # print(envs)
    >>> envs == [
    ...     {'name': 'base', 'refs': set()},
    ...     {'name': 'py27', 'refs': set()},
    ...     {'name': 'test', 'refs': {'base'}},
    ...     {'name': 'local', 'refs': {'test'}},
    ...     {'name': 'local27', 'refs': {'test', 'py27'}},
    ...     {'name': 'testwin', 'refs': {'test'}},
    ... ]
    True
    """
    in_paths = glob.glob(glob_pattern)
    names = {
        extract_env_name(path): path
        for path in in_paths
    }
    return order_by_refs([
        {'name': name, 'refs': Environment.parse_references(in_path)}
        for name, in_path in names.items()
    ])


def extract_env_name(file_path):
    """Return environment name for given requirements file path"""
    return os.path.splitext(os.path.basename(file_path))[0]


def order_by_refs(envs):
    """
    Return topologicaly sorted list of environments.
    I.e. all referenced environments are placed before their references.
    """
    topology = {
        env['name']: set(env['refs'])
        for env in envs
    }
    by_name = {
        env['name']: env
        for env in envs
    }
    return [
        by_name[name]
        for name in toposort_flatten(topology)
    ]


@cli.command()
@click.pass_context
def verify(ctx):
    """
    For each environment verify hash comments and report failures.
    If any failure occured, exit with code 1.
    """
    env_confs = discover(
        os.path.join(
            OPTIONS['base_dir'],
            '*.' + OPTIONS['in_ext'],
        )
    )
    success = True
    for conf in env_confs:
        env = Environment(name=conf['name'])
        logger.info("Verifying that %s was generated from %s.",
                    env.outfile, env.infile)
        current_comment = generate_hash_comment(env.infile)
        existing_comment = parse_hash_comment(env.outfile)
        if current_comment == existing_comment:
            logger.info("Success - comments match.")
        else:
            logger.error("FAILURE!")
            logger.error("Expecting: %s", current_comment.strip())
            logger.error("Found:     %s", existing_comment.strip())
            success = False
    if not success:
        ctx.exit(1)


def generate_hash_comment(file_path):
    """
    Read file with given file_path and return string of format

        # SHA1:da39a3ee5e6b4b0d3255bfef95601890afd80709

    which is hex representation of SHA1 file content hash
    """
    with open(file_path, 'rb') as fp:
        hexdigest = hashlib.sha1(fp.read().strip()).hexdigest()
    return "# SHA1:{0}\n".format(hexdigest)


def parse_hash_comment(file_path):
    """
    Read file with given file_path line by line,
    return the first line that starts with "# SHA1:", like this:

        # SHA1:da39a3ee5e6b4b0d3255bfef95601890afd80709
    """
    with open(file_path) as fp:
        for line in fp:
            if line.startswith("# SHA1:"):
                return line
    return None
