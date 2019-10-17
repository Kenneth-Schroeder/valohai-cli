import gzip
import os
import subprocess
import tarfile
import tempfile
import fnmatch
from collections import namedtuple
from subprocess import check_output

import click

from valohai_cli.exceptions import ConfigurationError, PackageTooLarge
from valohai_cli.messages import info, warn
from valohai_cli.utils.file_size_format import filesizeformat

FILE_SIZE_WARN_THRESHOLD = 50 * 1024 * 1024
FILE_COUNT_HARD_THRESHOLD = 10000
UNCOMPRESSED_PACKAGE_SIZE_SOFT_THRESHOLD = 150 * 1024 * 1024
COMPRESSED_PACKAGE_SIZE_HARD_THRESHOLD = 1000 * 1024 * 1024

# We guess that Gzip may help halve the package size -
# if the package is actually all source code, it will probably help more.
UNCOMPRESSED_PACKAGE_SIZE_HARD_THRESHOLD = COMPRESSED_PACKAGE_SIZE_HARD_THRESHOLD / 0.5

PACKAGE_SIZE_HELP = '''
It's generally not a good idea to have large files in your working copy,
as most version control systems, Git included, are optimized to work with
code, not data.

Data files such as pretraining data or checkpoint files should be managed
with the Valohai inputs system instead for increased performance.

If you are using Git, consider `git rm`ing the large files and adding
their patterns to your `.gitignore` file.

You can disable this validation with the `--no-validate-adhoc` option.
'''

PackageFileInfo = namedtuple('PackageFileInfo', ('source_path', 'stat'))


def package_directory(dir, progress=False, validate=True):
    file_stats = get_files_for_package(dir)

    if 'valohai.yaml' not in file_stats:
        raise ConfigurationError('valohai.yaml missing from {}'.format(dir))

    if validate:
        package_size_warnings = validate_package_size(file_stats)
        if package_size_warnings:
            for warning in package_size_warnings:
                click.secho('* ' + warning, err=True)
            click.secho(PACKAGE_SIZE_HELP, err=True)
            click.confirm('Continue packaging anyway?', default=True, abort=True, prompt_suffix='', err=True)

    with tempfile.NamedTemporaryFile(delete=False, suffix='.tgz', prefix='valohai-cli-') as fp:
        package_files_into(fp, file_stats, progress=progress)
        total_compressed_size = fp.tell()

    if validate and total_compressed_size >= COMPRESSED_PACKAGE_SIZE_HARD_THRESHOLD:
        raise PackageTooLarge(
            'The total compressed size of the package is {size}, which exceeds the threshold {threshold}'.format(
                size=filesizeformat(total_compressed_size),
                threshold=filesizeformat(COMPRESSED_PACKAGE_SIZE_HARD_THRESHOLD),
            ))
    return fp.name


def package_files_into(dest_fp, file_stats, progress=False):
    """
    Package (gzipped tarball) files from `file_stats` (which is a dict mapping names within the package
    to their PackageFileInfo tuples) into the open writable binary file `dest_fp`.

    The dict could look like this:

    {
      'valohai.yaml': PackageFileInfo(source_path='/my/tmp/valohai.yaml', stat=...),
      'train.py': PackageFileInfo(source_path='/my/somewhere/else/train.py', stat=...),
      'data/foo.dat': PackageFileInfo(source_path='/tmp/big.data', stat=...),
    }

    :param dest_fp: Target file descriptor
    :param file_stats: Dict of files to infos
    :param progress: Whether to show progress
    :return:
    """

    files = sorted(file_stats.keys())

    # Manually creating the gzipfile to force mtime to 0.
    with gzip.GzipFile('data.tar', mode='w', fileobj=dest_fp, mtime=0) as gzf:
        with tarfile.open(name='data.tar', mode='w', fileobj=gzf) as tarball:
            progress_bar = click.progressbar(
                files,
                show_pos=True,
                item_show_func=lambda i: ('Packaging: %s' % i if i else ''),
                width=0,
            )
            if not progress:
                progress_bar.is_hidden = True

            with progress_bar:
                for file in progress_bar:
                    pfi = file_stats[file]
                    if os.path.isfile(pfi.source_path):
                        tarball.add(name=pfi.source_path, arcname=file)
    dest_fp.flush()


def get_files_for_package(dir, allow_git=True, ignore=[]):
    """
    Get files to package for ad-hoc packaging from the file system.

    :param dir: The source directory. Probably a working copy root or similar.
    :param allow_git: Whether to allow usage of `git ls-files`, if available, for packaging.
    :param ignore: List of ignored patterns.
    :return:
    """
    files = None
    files_and_paths = None
    if allow_git and os.path.exists(os.path.join(dir, '.git')):
        # We have .git, so we can try to use Git to figure out a file list of nonignored files
        try:
            files = [
                line.decode('utf-8')
                for line
                in check_output('git ls-files --exclude-standard -ocz', cwd=dir, shell=True).split(b'\0')
                if line and not line.startswith(b'.') and is_valid_path(line.decode('utf-8'), ignore)
            ]
            files_and_paths = [
                (file, os.path.join(dir, file))
                for file
                in files
            ]
            info('Used git to find {n} files to package'.format(n=len(files_and_paths)))
        except subprocess.CalledProcessError as cpe:
            warn('.git exists, but we could not use git ls-files (error %d), falling back to non-git' % cpe.returncode)

    if files_and_paths is None:
        # We failed to use git for packaging, or didn't want to -
        # just package up everything that doesn't have a . prefix and not included in ignore list
        files = []
        for dirpath, dirnames, filenames in os.walk(dir):
            dirnames[:] = [dirname for dirname in dirnames if not dirname.startswith('.')]
            files.extend([
                os.path.join(dirpath, filename) for filename in filenames
                if not filename.startswith('.') and is_valid_path(os.path.join(dirpath, filename), ignore)
            ])
            if len(files) > FILE_COUNT_HARD_THRESHOLD:
                raise PackageTooLarge(
                    'Trying to package too many files (threshold: {threshold}).'.format(
                        threshold=FILE_COUNT_HARD_THRESHOLD,
                    ))

        files_and_paths = [
            (filepath[len(dir):].lstrip(os.sep), filepath)
            for filepath
            in files
        ]

        info('Git not available, found {n} files to package'.format(n=len(files_and_paths)))

    output_stats = {}
    for file, file_path in files_and_paths:
        output_stats[file] = PackageFileInfo(source_path=file_path, stat=os.stat(file_path))
    return output_stats


def validate_package_size(file_stats):
    """
    :type file_stats: Dict[str, PackageFileInfo]
    """
    warnings = []
    total_uncompressed_size = 0
    for file, pfi in sorted(file_stats.items()):
        stat = pfi.stat
        total_uncompressed_size += stat.st_size
        if stat.st_size >= FILE_SIZE_WARN_THRESHOLD:
            warnings.append('Large file {file}: {size}'.format(
                file=file,
                size=filesizeformat(stat.st_size),
            ))
    if total_uncompressed_size >= UNCOMPRESSED_PACKAGE_SIZE_SOFT_THRESHOLD:
        warnings.append('The total uncompressed size of the package is {size}'.format(
            size=filesizeformat(total_uncompressed_size),
        ))
    if total_uncompressed_size >= UNCOMPRESSED_PACKAGE_SIZE_HARD_THRESHOLD:
        raise PackageTooLarge(
            'The total uncompressed size of the package is {size}, which exceeds the threshold {threshold}'.format(
                size=filesizeformat(total_uncompressed_size),
                threshold=filesizeformat(UNCOMPRESSED_PACKAGE_SIZE_HARD_THRESHOLD),
            ))
    return warnings


def is_valid_path(path, ignore):
    for ignored in ignore:
        if fnmatch.fnmatch(path, ignored) or ignored in path:
            return False
    return True
