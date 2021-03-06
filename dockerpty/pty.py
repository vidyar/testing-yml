# dockerpty: pty.py
#
# Copyright 2014 Chris Corbyn <chris@w3style.co.uk>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import signal

import dockerpty.io as io
import dockerpty.tty as tty


class WINCHHandler(object):
    """
    WINCH Signal handler to keep the PTY correctly sized.
    """

    def __init__(self, pty):
        """
        Initialize a new WINCH handler for the given PTY.

        Initializing a handler has no immediate side-effects. The `start()`
        method must be invoked for the signals to be trapped.
        """

        self.pty = pty
        self.original_handler = None


    def __enter__(self):
        """
        Invoked on entering a `with` block.
        """

        self.start()
        return self


    def __exit__(self, *_):
        """
        Invoked on exiting a `with` block.
        """

        self.stop()


    def start(self):
        """
        Start trapping WINCH signals and resizing the PTY.

        This method saves the previous WINCH handler so it can be restored on
        `stop()`.
        """

        def handle(signum, frame):
            if signum == signal.SIGWINCH:
                self.pty.resize()

        self.original_handler = signal.signal(signal.SIGWINCH, handle)


    def stop(self):
        """
        Stop trapping WINCH signals and restore the previous WINCH handler.
        """

        if self.original_handler is not None:
            signal.signal(signal.SIGWINCH, self.original_handler)


class PseudoTerminal(object):
    """
    Wraps the pseudo-TTY (PTY) allocated to a docker container.

    The PTY is managed via the current process' TTY until it is closed.

    Example:

        import docker
        from dockerpty import PseudoTerminal

        client = docker.Client()
        container = client.create_container(
            image='busybox:latest',
            stdin_open=True,
            tty=True,
            command='/bin/sh',
        )
        client.start(container)

        # hijacks the current tty until the pty is closed
        PseudoTerminal(client, container).start()

    Care is taken to ensure all file descriptors are restored on exit. For
    example, you can attach to a running container from within a Python REPL
    and when the container exits, the user will be returned to the Python REPL
    without adverse effects.
    """


    def __init__(self, client, container):
        """
        Initialize the PTY using the docker.Client instance and container dict.
        """

        self.client = client
        self.container = container
        self.raw = None


    def start(self):
        """
        Present the PTY of the container inside the current process.

        The container must be started before this method is invoked.

        This will take over the current process' TTY until the container's PTY
        is closed.
        """

        info = self.container_info()
        if not info['State']['Running']:
            raise Exception('Cannot attach to a stopped container')

        pty_stdin, pty_stdout, pty_stderr = self.sockets()

        pumps = [
            io.Pump(sys.stdin, pty_stdin),
            io.Pump(pty_stdout, sys.stdout),
            io.Pump(pty_stderr, sys.stderr),
        ]

        try:
            flags = [io.set_blocking(p, False) for p in pumps]
            with WINCHHandler(self):
                self._hijack_tty(pumps)
        finally:
            for (pump, flag) in zip(pumps, flags):
                io.set_blocking(pump, flag)


    def israw(self):
        """
        Returns True if the PTY should operate in raw mode.

        If the container was not started with tty=True, this will return False.
        """

        if self.raw is None:
            info = self.container_info()
            self.raw = sys.stdout.isatty() and info['Config']['Tty']

        return self.raw


    def sockets(self):
        """
        Returns a tuple of sockets connected to the pty (stdin,stdout,stderr).
        """

        def attach_socket(key):
            return self.client.attach_socket(
                self.container,
                {key: 1, 'stream': 1},
            )

        return map(attach_socket, ('stdin', 'stdout', 'stderr'))


    def resize(self, size=None):
        """
        Resize the container's PTY.

        If `size` is not None, it must be a tuple of (height,width), otherwise
        it will be determined by the size of the current TTY.
        """

        if not self.israw():
            return

        size = size or tty.size(sys.stdout)

        if size is not None:
            rows, cols = size
            try:
                self.client.resize(self.container, height=rows, width=cols)
            except IOError: # Container already exited
                pass


    def container_info(self):
        """
        Thin wrapper around client.inspect_container().
        """

        return self.client.inspect_container(self.container)


    def _hijack_tty(self, pumps):
        with tty.Terminal(sys.stdin, raw=self.israw()):
            self.resize()
            while True:
                ready = io.select(pumps, timeout=1)
                if not all([p.flush() is not None for p in ready]):
                    break
