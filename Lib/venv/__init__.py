"""
Virtual environment (venv) package for Python. Based on PEP 405.

Copyright (C) 2011-2014 Vinay Sajip.
Licensed to the PSF under a contributor agreement.
"""
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import sysconfig
import types
import shlex


DEFAULT_NAME = '.venv'
_CFG_FILE_NAME = 'pyvenv.cfg'
CORE_VENV_DEPS = ('pip',)
logger = logging.getLogger(__name__)


class EnvBuilder:
    """
    This class exists to allow virtual environment creation to be
    customized. The constructor parameters determine the builder's
    behaviour when called upon to create a virtual environment.

    By default, the builder makes the system (global) site-packages dir
    *un*available to the created environment.

    If invoked using the Python -m option, the default is to use copying
    on Windows platforms but symlinks elsewhere. If instantiated some
    other way, the default is to *not* use symlinks.

    :param system_site_packages: If True, the system (global) site-packages
                                 dir is available to created environments.
    :param clear: If True, delete the contents of the environment directory if
                  it already exists, before environment creation.
    :param symlinks: If True, attempt to symlink rather than copy files into
                     virtual environment.
    :param upgrade: If True, upgrade an existing virtual environment.
    :param with_pip: If True, ensure pip is installed in the virtual
                     environment
    :param prompt: Alternative terminal prefix for the environment.
    :param upgrade_deps: Update the base venv modules to the latest on PyPI
    :param scm_ignore_files: Create ignore files for the SCMs specified by the
                             iterable.
    """

    def __init__(self, system_site_packages=False, clear=False,
                 symlinks=False, upgrade=False, with_pip=False, prompt=None,
                 upgrade_deps=False, *, scm_ignore_files=frozenset()):
        self.system_site_packages = system_site_packages
        self.clear = clear
        self.symlinks = symlinks
        self.upgrade = upgrade
        self.with_pip = with_pip
        self.orig_prompt = prompt
        if prompt == '.':  # see bpo-38901
            prompt = os.path.basename(os.getcwd())
        self.prompt = prompt
        self.upgrade_deps = upgrade_deps
        self.scm_ignore_files = frozenset(map(str.lower, scm_ignore_files))

    def create(self, env_dir=DEFAULT_NAME, *, project_root=None):
        """
        Create a virtual environment in a directory.

        Optionally, write a ``.venv`` file at *project_root* if necessary
        (i.e. *env_dir* is not already being placed in ``DEFAULT_NAME`` or
        the parent directory of *end_dir* is different from *project_root*).

        :param env_dir: The target directory to create an environment in.
        :param project_root: The root of the project where a ``.venv`` file
                             will be created if necessary.

        """
        env_dir = os.path.abspath(env_dir)
        context = self.ensure_directories(env_dir)
        for scm in self.scm_ignore_files:
            getattr(self, f"create_{scm}_ignore_file")(context)
        # See issue 24875. We need system_site_packages to be False
        # until after pip is installed.
        true_system_site_packages = self.system_site_packages
        self.system_site_packages = False
        self.create_configuration(context)
        self.setup_python(context)
        if self.with_pip:
            self._setup_pip(context)
        if not self.upgrade:
            self.setup_scripts(context)
            self.post_setup(context)
        if true_system_site_packages:
            # We had set it to False before, now
            # restore it and rewrite the configuration
            self.system_site_packages = True
            self.create_configuration(context)
        if self.upgrade_deps:
            self.upgrade_dependencies(context)
        if project_root is not None:
            env_path = pathlib.Path(env_dir)
            project_path = pathlib.Path(project_root)
            if (env_path.name != DEFAULT_NAME
                    or env_path.parent != project_path):
                self.write_redirect_file(project_path, env_path)

    def clear_directory(self, path):
        for fn in os.listdir(path):
            fn = os.path.join(path, fn)
            if os.path.islink(fn) or os.path.isfile(fn):
                os.remove(fn)
            elif os.path.isdir(fn):
                shutil.rmtree(fn)

    def _venv_path(self, env_dir, name):
        vars = {
            'base': env_dir,
            'platbase': env_dir,
        }
        return sysconfig.get_path(name, scheme='venv', vars=vars)

    @classmethod
    def _same_path(cls, path1, path2):
        """Check whether two paths appear the same.

        Whether they refer to the same file is irrelevant; we're testing for
        whether a human reader would look at the path string and easily tell
        that they're the same file.
        """
        if sys.platform == 'win32':
            if os.path.normcase(path1) == os.path.normcase(path2):
                return True
            # gh-90329: Don't display a warning for short/long names
            import _winapi
            try:
                path1 = _winapi.GetLongPathName(os.fsdecode(path1))
            except OSError:
                pass
            try:
                path2 = _winapi.GetLongPathName(os.fsdecode(path2))
            except OSError:
                pass
            if os.path.normcase(path1) == os.path.normcase(path2):
                return True
            return False
        else:
            return path1 == path2

    def ensure_directories(self, env_dir):
        """
        Create the directories for the environment.

        Returns a context object which holds paths in the environment,
        for use by subsequent logic.
        """

        def create_if_needed(d):
            if not os.path.exists(d):
                os.makedirs(d)
            elif os.path.islink(d) or os.path.isfile(d):
                raise ValueError('Unable to create directory %r' % d)

        if os.pathsep in os.fspath(env_dir):
            raise ValueError(f'Refusing to create a venv in {env_dir} because '
                             f'it contains the PATH separator {os.pathsep}.')
        if os.path.exists(env_dir) and self.clear:
            self.clear_directory(env_dir)
        context = types.SimpleNamespace()
        context.env_dir = env_dir
        context.env_name = os.path.split(env_dir)[1]
        context.prompt = self.prompt if self.prompt is not None else context.env_name
        create_if_needed(env_dir)
        executable = sys._base_executable
        if not executable:  # see gh-96861
            raise ValueError('Unable to determine path to the running '
                             'Python interpreter. Provide an explicit path or '
                             'check that your PATH environment variable is '
                             'correctly set.')
        dirname, exename = os.path.split(os.path.abspath(executable))
        if sys.platform == 'win32':
            # Always create the simplest name in the venv. It will either be a
            # link back to executable, or a copy of the appropriate launcher
            _d = '_d' if os.path.splitext(exename)[0].endswith('_d') else ''
            exename = f'python{_d}.exe'
        context.executable = executable
        context.python_dir = dirname
        context.python_exe = exename
        binpath = self._venv_path(env_dir, 'scripts')
        libpath = self._venv_path(env_dir, 'purelib')
        platlibpath = self._venv_path(env_dir, 'platlib')

        # PEP 405 says venvs should create a local include directory.
        # See https://peps.python.org/pep-0405/#include-files
        # XXX: This directory is not exposed in sysconfig or anywhere else, and
        #      doesn't seem to be utilized by modern packaging tools. We keep it
        #      for backwards-compatibility, and to follow the PEP, but I would
        #      recommend against using it, as most tooling does not pass it to
        #      compilers. Instead, until we standardize a site-specific include
        #      directory, I would recommend installing headers as package data,
        #      and providing some sort of API to get the include directories.
        #      Example: https://numpy.org/doc/2.1/reference/generated/numpy.get_include.html
        incpath = os.path.join(env_dir, 'Include' if os.name == 'nt' else 'include')

        context.inc_path = incpath
        create_if_needed(incpath)
        context.lib_path = libpath
        create_if_needed(libpath)
        context.platlib_path = platlibpath
        create_if_needed(platlibpath)
        context.bin_path = binpath
        context.bin_name = os.path.relpath(binpath, env_dir)
        context.env_exe = os.path.join(binpath, exename)
        create_if_needed(binpath)
        # Assign and update the command to use when launching the newly created
        # environment, in case it isn't simply the executable script (e.g. bpo-45337)
        context.env_exec_cmd = context.env_exe
        if sys.platform == 'win32':
            # bpo-45337: Fix up env_exec_cmd to account for file system redirections.
            # Some redirects only apply to CreateFile and not CreateProcess
            real_env_exe = os.path.realpath(context.env_exe)
            if not self._same_path(real_env_exe, context.env_exe):
                logger.warning('Actual environment location may have moved due to '
                               'redirects, links or junctions.\n'
                               '  Requested location: "%s"\n'
                               '  Actual location:    "%s"',
                               context.env_exe, real_env_exe)
                context.env_exec_cmd = real_env_exe
        return context

    def write_redirect_file(self, project_root, env_dir):
        """
        Create a ``.venv`` file at *project_root* that points to *venv_dir*.

        :param project_root: The root of the project where the ``.venv`` file
                             will be created.
        :param env_dir: The directory of the virtual environment.
        """
        write_redirect_file(project_root, env_dir)

    def create_configuration(self, context):
        """
        Create a configuration file indicating where the environment's Python
        was copied from, and whether the system site-packages should be made
        available in the environment.

        :param context: The information for the environment creation request
                        being processed.
        """
        context.cfg_path = path = os.path.join(context.env_dir, _CFG_FILE_NAME)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('home = %s\n' % context.python_dir)
            if self.system_site_packages:
                incl = 'true'
            else:
                incl = 'false'
            f.write('include-system-site-packages = %s\n' % incl)
            f.write('version = %d.%d.%d\n' % sys.version_info[:3])
            if self.prompt is not None:
                f.write(f'prompt = {self.prompt!r}\n')
            f.write('executable = %s\n' % os.path.realpath(sys.executable))
            args = []
            nt = os.name == 'nt'
            if nt and self.symlinks:
                args.append('--symlinks')
            if not nt and not self.symlinks:
                args.append('--copies')
            if not self.with_pip:
                args.append('--without-pip')
            if self.system_site_packages:
                args.append('--system-site-packages')
            if self.clear:
                args.append('--clear')
            if self.upgrade:
                args.append('--upgrade')
            if self.upgrade_deps:
                args.append('--upgrade-deps')
            if self.orig_prompt is not None:
                args.append(f'--prompt="{self.orig_prompt}"')
            if not self.scm_ignore_files:
                args.append('--without-scm-ignore-files')

            args.append(context.env_dir)
            args = ' '.join(args)
            f.write(f'command = {sys.executable} -m venv {args}\n')

    def symlink_or_copy(self, src, dst, relative_symlinks_ok=False):
        """
        Try symlinking a file, and if that fails, fall back to copying.
        (Unused on Windows, because we can't just copy a failed symlink file: we
        switch to a different set of files instead.)
        """
        assert os.name != 'nt'
        force_copy = not self.symlinks
        if not force_copy:
            try:
                if not os.path.islink(dst):  # can't link to itself!
                    if relative_symlinks_ok:
                        assert os.path.dirname(src) == os.path.dirname(dst)
                        os.symlink(os.path.basename(src), dst)
                    else:
                        os.symlink(src, dst)
            except Exception:   # may need to use a more specific exception
                logger.warning('Unable to symlink %r to %r', src, dst)
                force_copy = True
        if force_copy:
            shutil.copyfile(src, dst)

    def create_git_ignore_file(self, context):
        """
        Create a .gitignore file in the environment directory.

        The contents of the file cause the entire environment directory to be
        ignored by git.
        """
        gitignore_path = os.path.join(context.env_dir, '.gitignore')
        with open(gitignore_path, 'w', encoding='utf-8') as file:
            file.write('# Created by venv; '
                       'see https://docs.python.org/3/library/venv.html\n')
            file.write('*\n')

    if os.name != 'nt':
        def setup_python(self, context):
            """
            Set up a Python executable in the environment.

            :param context: The information for the environment creation request
                            being processed.
            """
            binpath = context.bin_path
            path = context.env_exe
            copier = self.symlink_or_copy
            copier(context.executable, path)
            if not os.path.islink(path):
                os.chmod(path, 0o755)
            for suffix in ('python', 'python3',
                           f'python3.{sys.version_info[1]}'):
                path = os.path.join(binpath, suffix)
                if not os.path.exists(path):
                    # Issue 18807: make copies if
                    # symlinks are not wanted
                    copier(context.env_exe, path, relative_symlinks_ok=True)
                    if not os.path.islink(path):
                        os.chmod(path, 0o755)

    else:
        def setup_python(self, context):
            """
            Set up a Python executable in the environment.

            :param context: The information for the environment creation request
                            being processed.
            """
            binpath = context.bin_path
            dirname = context.python_dir
            exename = os.path.basename(context.env_exe)
            exe_stem = os.path.splitext(exename)[0]
            exe_d = '_d' if os.path.normcase(exe_stem).endswith('_d') else ''
            if sysconfig.is_python_build():
                scripts = dirname
            else:
                scripts = os.path.join(os.path.dirname(__file__),
                                       'scripts', 'nt')
            if not sysconfig.get_config_var("Py_GIL_DISABLED"):
                python_exe = os.path.join(dirname, f'python{exe_d}.exe')
                pythonw_exe = os.path.join(dirname, f'pythonw{exe_d}.exe')
                link_sources = {
                    'python.exe': python_exe,
                    f'python{exe_d}.exe': python_exe,
                    'pythonw.exe': pythonw_exe,
                    f'pythonw{exe_d}.exe': pythonw_exe,
                }
                python_exe = os.path.join(scripts, f'venvlauncher{exe_d}.exe')
                pythonw_exe = os.path.join(scripts, f'venvwlauncher{exe_d}.exe')
                copy_sources = {
                    'python.exe': python_exe,
                    f'python{exe_d}.exe': python_exe,
                    'pythonw.exe': pythonw_exe,
                    f'pythonw{exe_d}.exe': pythonw_exe,
                }
            else:
                exe_t = f'3.{sys.version_info[1]}t'
                python_exe = os.path.join(dirname, f'python{exe_t}{exe_d}.exe')
                pythonw_exe = os.path.join(dirname, f'pythonw{exe_t}{exe_d}.exe')
                link_sources = {
                    'python.exe': python_exe,
                    f'python{exe_d}.exe': python_exe,
                    f'python{exe_t}.exe': python_exe,
                    f'python{exe_t}{exe_d}.exe': python_exe,
                    'pythonw.exe': pythonw_exe,
                    f'pythonw{exe_d}.exe': pythonw_exe,
                    f'pythonw{exe_t}.exe': pythonw_exe,
                    f'pythonw{exe_t}{exe_d}.exe': pythonw_exe,
                }
                python_exe = os.path.join(scripts, f'venvlaunchert{exe_d}.exe')
                pythonw_exe = os.path.join(scripts, f'venvwlaunchert{exe_d}.exe')
                copy_sources = {
                    'python.exe': python_exe,
                    f'python{exe_d}.exe': python_exe,
                    f'python{exe_t}.exe': python_exe,
                    f'python{exe_t}{exe_d}.exe': python_exe,
                    'pythonw.exe': pythonw_exe,
                    f'pythonw{exe_d}.exe': pythonw_exe,
                    f'pythonw{exe_t}.exe': pythonw_exe,
                    f'pythonw{exe_t}{exe_d}.exe': pythonw_exe,
                }

            do_copies = True
            if self.symlinks:
                do_copies = False
                # For symlinking, we need all the DLLs to be available alongside
                # the executables.
                link_sources.update({
                    f: os.path.join(dirname, f) for f in os.listdir(dirname)
                    if os.path.normcase(f).startswith(('python', 'vcruntime'))
                    and os.path.normcase(os.path.splitext(f)[1]) == '.dll'
                })

                to_unlink = []
                for dest, src in link_sources.items():
                    dest = os.path.join(binpath, dest)
                    try:
                        os.symlink(src, dest)
                        to_unlink.append(dest)
                    except OSError:
                        logger.warning('Unable to symlink %r to %r', src, dest)
                        do_copies = True
                        for f in to_unlink:
                            try:
                                os.unlink(f)
                            except OSError:
                                logger.warning('Failed to clean up symlink %r',
                                               f)
                        logger.warning('Retrying with copies')
                        break

            if do_copies:
                for dest, src in copy_sources.items():
                    dest = os.path.join(binpath, dest)
                    try:
                        shutil.copy2(src, dest)
                    except OSError:
                        logger.warning('Unable to copy %r to %r', src, dest)

            if sysconfig.is_python_build():
                # copy init.tcl
                for root, dirs, files in os.walk(context.python_dir):
                    if 'init.tcl' in files:
                        tcldir = os.path.basename(root)
                        tcldir = os.path.join(context.env_dir, 'Lib', tcldir)
                        if not os.path.exists(tcldir):
                            os.makedirs(tcldir)
                        src = os.path.join(root, 'init.tcl')
                        dst = os.path.join(tcldir, 'init.tcl')
                        shutil.copyfile(src, dst)
                        break

    def _call_new_python(self, context, *py_args, **kwargs):
        """Executes the newly created Python using safe-ish options"""
        # gh-98251: We do not want to just use '-I' because that masks
        # legitimate user preferences (such as not writing bytecode). All we
        # really need is to ensure that the path variables do not overrule
        # normal venv handling.
        args = [context.env_exec_cmd, *py_args]
        kwargs['env'] = env = os.environ.copy()
        env['VIRTUAL_ENV'] = context.env_dir
        env.pop('PYTHONHOME', None)
        env.pop('PYTHONPATH', None)
        kwargs['cwd'] = context.env_dir
        kwargs['executable'] = context.env_exec_cmd
        subprocess.check_output(args, **kwargs)

    def _setup_pip(self, context):
        """Installs or upgrades pip in a virtual environment"""
        self._call_new_python(context, '-m', 'ensurepip', '--upgrade',
                              '--default-pip', stderr=subprocess.STDOUT)

    def setup_scripts(self, context):
        """
        Set up scripts into the created environment from a directory.

        This method installs the default scripts into the environment
        being created. You can prevent the default installation by overriding
        this method if you really need to, or if you need to specify
        a different location for the scripts to install. By default, the
        'scripts' directory in the venv package is used as the source of
        scripts to install.
        """
        path = os.path.abspath(os.path.dirname(__file__))
        path = os.path.join(path, 'scripts')
        self.install_scripts(context, path)

    def post_setup(self, context):
        """
        Hook for post-setup modification of the venv. Subclasses may install
        additional packages or scripts here, add activation shell scripts, etc.

        :param context: The information for the environment creation request
                        being processed.
        """
        pass

    def replace_variables(self, text, context):
        """
        Replace variable placeholders in script text with context-specific
        variables.

        Return the text passed in , but with variables replaced.

        :param text: The text in which to replace placeholder variables.
        :param context: The information for the environment creation request
                        being processed.
        """
        replacements = {
            '__VENV_DIR__': context.env_dir,
            '__VENV_NAME__': context.env_name,
            '__VENV_PROMPT__': context.prompt,
            '__VENV_BIN_NAME__': context.bin_name,
            '__VENV_PYTHON__': context.env_exe,
        }

        def quote_ps1(s):
            """
            This should satisfy PowerShell quoting rules [1], unless the quoted
            string is passed directly to Windows native commands [2].
            [1]: https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules
            [2]: https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_parsing#passing-arguments-that-contain-quote-characters
            """
            s = s.replace("'", "''")
            return f"'{s}'"

        def quote_bat(s):
            return s

        # gh-124651: need to quote the template strings properly
        quote = shlex.quote
        script_path = context.script_path
        if script_path.endswith('.ps1'):
            quote = quote_ps1
        elif script_path.endswith('.bat'):
            quote = quote_bat
        else:
            # fallbacks to POSIX shell compliant quote
            quote = shlex.quote

        replacements = {key: quote(s) for key, s in replacements.items()}
        for key, quoted in replacements.items():
            text = text.replace(key, quoted)
        return text

    def install_scripts(self, context, path):
        """
        Install scripts into the created environment from a directory.

        :param context: The information for the environment creation request
                        being processed.
        :param path:    Absolute pathname of a directory containing script.
                        Scripts in the 'common' subdirectory of this directory,
                        and those in the directory named for the platform
                        being run on, are installed in the created environment.
                        Placeholder variables are replaced with environment-
                        specific values.
        """
        binpath = context.bin_path
        plen = len(path)
        if os.name == 'nt':
            def skip_file(f):
                f = os.path.normcase(f)
                return (f.startswith(('python', 'venv'))
                        and f.endswith(('.exe', '.pdb')))
        else:
            def skip_file(f):
                return False
        for root, dirs, files in os.walk(path):
            if root == path:  # at top-level, remove irrelevant dirs
                for d in dirs[:]:
                    if d not in ('common', os.name):
                        dirs.remove(d)
                continue  # ignore files in top level
            for f in files:
                if skip_file(f):
                    continue
                srcfile = os.path.join(root, f)
                suffix = root[plen:].split(os.sep)[2:]
                if not suffix:
                    dstdir = binpath
                else:
                    dstdir = os.path.join(binpath, *suffix)
                if not os.path.exists(dstdir):
                    os.makedirs(dstdir)
                dstfile = os.path.join(dstdir, f)
                if os.name == 'nt' and srcfile.endswith(('.exe', '.pdb')):
                    shutil.copy2(srcfile, dstfile)
                    continue
                with open(srcfile, 'rb') as f:
                    data = f.read()
                try:
                    context.script_path = srcfile
                    new_data = (
                        self.replace_variables(data.decode('utf-8'), context)
                            .encode('utf-8')
                    )
                except UnicodeError as e:
                    logger.warning('unable to copy script %r, '
                                   'may be binary: %s', srcfile, e)
                    continue
                if new_data == data:
                    shutil.copy(srcfile, dstfile)
                else:
                    with open(dstfile, 'wb') as f:
                        f.write(new_data)
                    shutil.copymode(srcfile, dstfile)

    def upgrade_dependencies(self, context):
        logger.debug(
            f'Upgrading {CORE_VENV_DEPS} packages in {context.bin_path}'
        )
        self._call_new_python(context, '-m', 'pip', 'install', '--upgrade',
                              *CORE_VENV_DEPS)


def create(env_dir=DEFAULT_NAME, system_site_packages=False, clear=False,
           symlinks=False, with_pip=False, prompt=None, upgrade_deps=False,
           *, scm_ignore_files=frozenset(), project_root=None):
    """Create a virtual environment in a directory."""
    builder = EnvBuilder(system_site_packages=system_site_packages,
                         clear=clear, symlinks=symlinks, with_pip=with_pip,
                         prompt=prompt, upgrade_deps=upgrade_deps,
                         scm_ignore_files=scm_ignore_files)
    builder.create(env_dir, project_root=project_root)


def read_redirect_file(project_root):
    """Read the path in the ``.venv`` redirect file at *project_root*."""
    redirect_file_path = pathlib.Path(project_root, DEFAULT_NAME)
    with open(redirect_file_path, 'r', encoding='utf-8') as f:
        first_line = next(iter(f))
    clean_first_line = first_line.removesuffix('\n').removesuffix('\r')
    path = pathlib.Path(project_root, clean_first_line).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Redirect file {redirect_file_path!r} points "
                                f"to non-existent {path!r}.")
    else:
        return path


def write_redirect_file(project_root, env_dir):
        """Create a ``.venv`` file at *project_root* that points to *venv_dir*."""
        redirect_file_path = os.path.join(project_root, DEFAULT_NAME)
        with open(redirect_file_path, 'w', encoding='utf-8') as f:
            f.write(os.fsdecode(env_dir))


def executable(dir, name=DEFAULT_NAME, *, traverse=False):
    """
    Find the Python executable in a virtual environment located in *dir* at ``DEFAULT_NAME``.

    If *traverse* is True, also look in parent directories of *dir* for a
    virtual environment.

    :param dir: The directory (to start) to look for a virtual environment in
                ``DEFAULT_NAME``.
    :param name: The name of the vritual environment directory or redirect file
                 to look for.
    :param traverse: Flag to control whether to traverse through parent
                     directories as part of the search.
    """
    location = pathlib.Path(dir)
    if not (venv_path := location / name).exists():
        if traverse and location.parent != location:
            return executable(location.parent, traverse=True)
        else:
            msg = f"No virtual environment found at {name!r} in {dir!r}"
            if traverse:
                msg += " or any parent directories"
            raise FileNotFoundError(msg)
    else:
        if venv_path.is_file():
            venv_path = read_redirect_file(location)

        # Check that this is an actual virtual environment.
        if not (cfg_path := venv_path / _CFG_FILE_NAME).is_file():
            raise FileNotFoundError(f"{_CFG_FILE_NAME!r} not found in {venv_path!r}")

        potential_paths = [venv_path / 'bin' / 'python',
                            venv_path / 'Scripts' / 'python.exe']
        for exe_path in potential_paths:
            if exe_path.is_file():
                return exe_path
        else:
            raise FileNotFoundError(f"No 'python' executable found in {venv_path!r}")

def main(args=None):
    import argparse

    parser = argparse.ArgumentParser(description='Creates virtual Python '
                                                 'environments in one or '
                                                 'more target '
                                                 'directories.',
                                     epilog='Once an environment has been '
                                            'created, you may wish to '
                                            'activate it, e.g. by '
                                            'sourcing an activate script '
                                            'in its bin directory.',
                                     color=True,
                                     )
    parser.add_argument('dirs', metavar='ENV_DIR', nargs='+',
                        default=[DEFAULT_NAME],
                        help='A directory to create the environment in '
                             f'(default is {DEFAULT_NAME!r}).')
    parser.add_argument("--project-root", dest="project_root", default=None,
                        help="The root of the project; used for creating a "
                             f"redirect file at {DEFAULT_NAME!r}, if necessary.")
    parser.add_argument('--system-site-packages', default=False,
                        action='store_true', dest='system_site',
                        help='Give the virtual environment access to the '
                             'system site-packages dir.')
    if os.name == 'nt':
        use_symlinks = False
    else:
        use_symlinks = True
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--symlinks', default=use_symlinks,
                       action='store_true', dest='symlinks',
                       help='Try to use symlinks rather than copies, '
                            'when symlinks are not the default for '
                            'the platform.')
    group.add_argument('--copies', default=not use_symlinks,
                       action='store_false', dest='symlinks',
                       help='Try to use copies rather than symlinks, '
                            'even when symlinks are the default for '
                            'the platform.')
    parser.add_argument('--clear', default=False, action='store_true',
                        dest='clear', help='Delete the contents of the '
                                           'environment directory if it '
                                           'already exists, before '
                                           'environment creation.')
    parser.add_argument('--upgrade', default=False, action='store_true',
                        dest='upgrade', help='Upgrade the environment '
                                             'directory to use this version '
                                             'of Python, assuming Python '
                                             'has been upgraded in-place.')
    parser.add_argument('--without-pip', dest='with_pip',
                        default=True, action='store_false',
                        help='Skips installing or upgrading pip in the '
                             'virtual environment (pip is bootstrapped '
                             'by default)')
    parser.add_argument('--prompt',
                        help='Provides an alternative prompt prefix for '
                             'this environment.')
    parser.add_argument('--upgrade-deps', default=False, action='store_true',
                        dest='upgrade_deps',
                        help=f'Upgrade core dependencies ({", ".join(CORE_VENV_DEPS)}) '
                             'to the latest version in PyPI')
    parser.add_argument('--without-scm-ignore-files', dest='scm_ignore_files',
                        action='store_const', const=frozenset(),
                        default=frozenset(['git']),
                        help='Skips adding SCM ignore files to the environment '
                             'directory (Git is supported by default).')
    options = parser.parse_args(args)
    if options.upgrade and options.clear:
        raise ValueError('you cannot supply --upgrade and --clear together.')
    elif len(options.dirs) > 1 and options.project_root is not None:
        raise ValueError('you cannot specify --project-root with multiple '
                         'environtment directories.')
    builder = EnvBuilder(system_site_packages=options.system_site,
                         clear=options.clear,
                         symlinks=options.symlinks,
                         upgrade=options.upgrade,
                         with_pip=options.with_pip,
                         prompt=options.prompt,
                         upgrade_deps=options.upgrade_deps,
                         scm_ignore_files=options.scm_ignore_files)
    for d in options.dirs:
        builder.create(d, project_root=options.project_root)


if __name__ == '__main__':
    rc = 1
    try:
        main()
        rc = 0
    except Exception as e:
        print('Error: %s' % e, file=sys.stderr)
    sys.exit(rc)
