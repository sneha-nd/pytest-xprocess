from __future__ import division, print_function

import sys
import os
import warnings
import abc
import functools
import itertools

from py import std
import psutil


# make map appear from the future
if sys.version_info < (3,):
    map = itertools.imap


class XProcessInfo:
    def __init__(self, path, name):
        self.name = name
        self.controldir = path.ensure(name, dir=1)
        self.logpath = self.controldir.join("xprocess.log")
        self.pidpath = self.controldir.join("xprocess.PID")
        self.pid = int(self.pidpath.read()) if self.pidpath.check() else None

    def terminate(self):
        # return codes:
        # 0   no work to do
        # 1   terminated
        # -1  failed to terminate

        if not self.pid or not self.isrunning():
            return 0

        timeout = 20

        try:
            proc = psutil.Process(self.pid)
            proc.terminate()
            try:
                proc.wait(timeout=timeout/2)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=timeout/2)
        except psutil.Error:
            return -1

        return 1

    def kill(self):
        warnings.warn("Use .terminate instead of .kill", DeprecationWarning, stacklevel=2)
        return self.terminate()

    def isrunning(self):
        if self.pid is None:
            return False
        try:
            proc = psutil.Process(self.pid)
        except psutil.NoSuchProcess:
            return False
        return proc.is_running()


class XProcess:
    def __init__(self, config, rootdir, log=None):
        self.config = config
        self.rootdir = rootdir
        class Log:
            def debug(self, msg, *args):
                print(msg % args)
        self.log = log or Log()

    def getinfo(self, name):
        """ return Process Info for the given external process. """
        return XProcessInfo(self.rootdir, name)

    def ensure(self, name, preparefunc, restart=False):
        """ returns (PID, logfile) from a newly started or already
            running process.

        @param name: name of the external process, used for caching info
                     across test runs.

        @param preparefunc:
                A subclass of ProcessStarter.

        @param restart: force restarting the process if it is running.

        @return: (PID, logfile) logfile will be seeked to the end if the
                 server was running, otherwise seeked to the line after
                 where the waitpattern matched.
        """
        from subprocess import Popen, STDOUT
        info = self.getinfo(name)
        if not restart and not info.isrunning():
            restart = True

        if restart:
            if info.pid is not None:
                info.terminate()
            controldir = info.controldir.ensure(dir=1)
            #controldir.remove()
            preparefunc = CompatStarter.wrap(preparefunc)
            starter = preparefunc(controldir, self)
            args = [str(x) for x in starter.args]
            self.log.debug("%s$ %s", controldir, " ".join(args))
            stdout = open(str(info.logpath), "wb", 0)
            kwargs = {'env': starter.env}
            if sys.platform == "win32":
                kwargs["startupinfo"] = sinfo = std.subprocess.STARTUPINFO()
                if sys.version_info >= (2,7):
                    sinfo.dwFlags |= std.subprocess.STARTF_USESHOWWINDOW
                    sinfo.wShowWindow |= std.subprocess.SW_HIDE
            else:
                kwargs["close_fds"] = True
                kwargs["preexec_fn"] = os.setpgrp  # no CONTROL-C
            popen = Popen(args, cwd=str(controldir),
                          stdout=stdout, stderr=STDOUT,
                          **kwargs)
            info.pid = pid = popen.pid
            info.pidpath.write(str(pid))
            self.log.debug("process %r started pid=%s", name, pid)
            stdout.close()
        f = info.logpath.open()
        if not restart:
            f.seek(0, 2)
        else:
            if not starter.wait(f):
                raise RuntimeError("Could not start process %s" % name)
            self.log.debug("%s process startup detected", name)
        logfiles = self.config.__dict__.setdefault("_extlogfiles", {})
        logfiles[name] = f
        self.getinfo(name)
        return info.pid, info.logpath

    def _infos(self):
        return (
            self.getinfo(p.basename)
            for p in self.rootdir.listdir()
        )

    def _xkill(self, tw):
        ret = 0
        for info in self._infos():
            termret = info.terminate()
            ret = ret or (termret==1)
            status = {
                1: 'TERMINATED',
                -1: 'FAILED TO TERMINATE',
                0: 'NO PROCESS FOUND',
            }[termret]
            tmpl = '{info.pid} {info.name} {status}'
            tw.line(tmpl.format(**locals()))
        return ret

    def _xshow(self, tw):
        for info in self._infos():
            running = 'LIVE' if info.isrunning() else 'DEAD'
            tmpl = '{info.pid} {info.name} {running} {info.logpath}'
            tw.line(tmpl.format(**locals()))
        return 0


class ProcessStarter(object):
    """
    Describes the characteristics of a process to start, waiting
    for a process to achieve a started state.
    """

    env = None
    """
    The environment in which to invoke the process.
    """

    def __init__(self, control_dir, process):
        self.control_dir = control_dir
        self.process = process

    @abc.abstractproperty
    def args(self):
        "The args to start the process"

    @abc.abstractproperty
    def pattern(self):
        "The pattern to match when the process has started"

    def wait(self, log_file):
        "Wait until the process is ready."
        lines = map(self.log_line, self.filter_lines(self.get_lines(log_file)))
        return any(
            std.re.search(self.pattern, line)
            for line in lines
        )

    def filter_lines(self, lines):
        # only consider the first 50 lines
        return itertools.islice(lines, 50)

    def log_line(self, line):
        self.process.log.debug(line)
        return line

    def get_lines(self, log_file):
        while True:
            line = log_file.readline()
            if not line:
                std.time.sleep(0.1)
            yield line


class CompatStarter(ProcessStarter):
    """
    A compatibility ProcessStarter to handle legacy preparefunc
    and warn of the deprecation.
    """

    # Define properties to satisfy the abstract property, though
    # they will be overridden at the instance.
    pattern = None
    args = None

    def __init__(self, preparefunc, control_dir, process):
        self.prep(*preparefunc(control_dir))
        super(CompatStarter, self).__init__(control_dir, process)

    def prep(self, wait, args, env=None):
        """
        Given the return value of a preparefunc, prepare this
        CompatStarter.
        """
        self.pattern = wait
        self.env = env
        self.args = args

        # wait is a function, supersedes the default behavior
        if callable(wait):
            self.wait = lambda lines: wait()

    @classmethod
    def wrap(self, starter_cls):
        """
        If starter_cls is not a ProcessStarter, assume it's the legacy
        preparefunc and return it bound to a CompatStarter.
        """
        if isinstance(starter_cls, type) and issubclass(starter_cls, ProcessStarter):
            return starter_cls
        depr_msg = 'Pass a ProcessStarter for preparefunc'
        warnings.warn(depr_msg, DeprecationWarning, stacklevel=3)
        return functools.partial(CompatStarter, starter_cls)
