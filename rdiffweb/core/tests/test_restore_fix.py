# rdiffweb, A web interface to rdiff-backup repositories
# Copyright (C) 2012-2025 rdiffweb contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import io
import os
import signal
import unittest
from unittest.mock import MagicMock, patch, call

from rdiffweb.core.restore import _wrap_close, RestoreException, pipe_restore
from rdiffweb.core.librdiff import unquote


class TestWrapClose(unittest.TestCase):
    """Test _wrap_close callback behavior and resource cleanup."""

    def setUp(self):
        self.success_count = 0
        self.failure_count = 0
        self.failure_exit_codes = []

    def success_cb(self):
        self.success_count += 1

    def failure_cb(self, exit_code):
        self.failure_count += 1
        self.failure_exit_codes.append(exit_code)

    def _create_wrapper(self, pid=12345):
        """Create a _wrap_close instance with a mock stream."""
        mock_stream = MagicMock()
        return _wrap_close(mock_stream, pid, self.success_cb, self.failure_cb), mock_stream

    @patch('os.waitpid')
    @patch('os.kill')
    def test_normal_success_flow(self, mock_kill, mock_waitpid):
        """Test normal flow: set_success() then close() with exit 0."""
        wrapper, stream = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper.set_success()
        wrapper.close()

        self.assertEqual(self.success_count, 1, 'success callback should be called once')
        self.assertEqual(self.failure_count, 0, 'failure callback should not be called')
        stream.close.assert_called_once()
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    @patch('os.waitpid')
    @patch('os.kill')
    def test_set_success_idempotent(self, mock_kill, mock_waitpid):
        """Test that multiple set_success() calls don't trigger multiple callbacks."""
        wrapper, stream = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper.set_success()
        wrapper.set_success()
        wrapper.set_success()
        wrapper.close()

        self.assertEqual(self.success_count, 1, 'success callback should be called only once')
        self.assertEqual(self.failure_count, 0)

    @patch('os.waitpid')
    @patch('os.kill')
    def test_close_idempotent(self, mock_kill, mock_waitpid):
        """Test that multiple close() calls don't trigger multiple callbacks."""
        wrapper, stream = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper.set_success()
        wrapper.close()
        wrapper.close()
        wrapper.close()

        self.assertEqual(self.success_count, 1, 'success callback should be called only once')
        self.assertEqual(self.failure_count, 0)
        stream.close.assert_called_once()

    @patch('os.waitpid')
    @patch('os.kill')
    def test_failure_with_nonzero_exit(self, mock_kill, mock_waitpid):
        """Test failure flow: non-zero exit code triggers failure callback."""
        wrapper, stream = self._create_wrapper()
        mock_waitpid.return_value = (12345, 1 << 8)

        wrapper.set_success()
        wrapper.close()

        self.assertEqual(self.success_count, 0, 'success callback should not be called on failure')
        self.assertEqual(self.failure_count, 1)
        self.assertEqual(self.failure_exit_codes, [1])

    @patch('os.waitpid')
    @patch('os.kill')
    def test_failure_without_set_success(self, mock_kill, mock_waitpid):
        """Test that without set_success(), even exit 0 doesn't trigger success callback."""
        wrapper, stream = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper.close()

        self.assertEqual(self.success_count, 0, 'success callback should not be called without set_success()')
        self.assertEqual(self.failure_count, 0, 'exit 0 without set_success should not trigger failure either')

    @patch('os.waitpid')
    @patch('os.kill')
    def test_context_manager_exception(self, mock_kill, mock_waitpid):
        """Test that exception in context triggers failure callback."""
        wrapper, stream = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        try:
            with wrapper:
                wrapper.set_success()
                raise ValueError('test error')
        except ValueError:
            pass

        self.assertEqual(self.success_count, 0, 'exception should prevent success callback')
        self.assertEqual(self.failure_count, 1, 'exception should trigger failure callback')
        self.assertEqual(self.failure_exit_codes, [None])

    @patch('os.waitpid')
    @patch('os.kill')
    def test_process_already_exited(self, mock_kill, mock_waitpid):
        """Test handling of ChildProcessError when process already exited."""
        wrapper, stream = self._create_wrapper()
        from errno import ECHILD
        mock_waitpid.side_effect = ChildProcessError(ECHILD, 'No child processes')

        wrapper.set_success()
        wrapper.close()

        self.assertEqual(self.success_count, 1)
        self.assertEqual(self.failure_count, 0)
        mock_kill.assert_called_once()

    @patch('os.waitpid')
    @patch('os.kill')
    def test_stream_close_exception(self, mock_kill, mock_waitpid):
        """Test that stream close exception doesn't prevent process cleanup."""
        wrapper, stream = self._create_wrapper()
        stream.close.side_effect = IOError('stream error')
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper.set_success()
        wrapper.close()

        self.assertEqual(self.success_count, 1)
        mock_kill.assert_called_once()
        mock_waitpid.assert_called_once()

    @patch('os.waitpid')
    @patch('os.kill')
    def test_success_callback_exception(self, mock_kill, mock_waitpid):
        """Test that callback exception doesn't break cleanup."""
        def bad_success_cb():
            self.success_count += 1
            raise RuntimeError('callback failed')

        mock_stream = MagicMock()
        wrapper = _wrap_close(mock_stream, 12345, bad_success_cb, self.failure_cb)
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper.set_success()
        wrapper.close()

        self.assertEqual(self.success_count, 1)
        mock_kill.assert_called_once()
        mock_waitpid.assert_called_once()


class TestPipeRestoreHeader(unittest.TestCase):
    """Test pipe_restore header processing and error propagation."""

    @patch('os.fork')
    @patch('os.pipe')
    def test_ok_header_triggers_set_success(self, mock_pipe, mock_fork):
        """Test that ok header causes set_success to be called."""
        r_fd, w_fd = 3, 4
        mock_pipe.return_value = (r_fd, w_fd)
        mock_fork.return_value = 99999

        mock_fileobj = MagicMock()
        mock_fileobj.readline.side_effect = [b'ok\n', b'']

        with patch('os.fdopen', return_value=mock_fileobj):
            with patch('os.close'):
                with patch('rdiffweb.core.restore._wrap_close') as mock_wrap:
                    mock_wrap_instance = MagicMock()
                    mock_wrap.return_value = mock_wrap_instance

                    result = pipe_restore(
                        b'/usr/bin/rdiff-backup',
                        b'/test/path',
                        1234567890,
                        'raw',
                        'utf-8',
                    )

                    mock_wrap_instance.set_success.assert_called_once()
                    self.assertEqual(result, mock_wrap_instance)

    @patch('os.fork')
    @patch('os.pipe')
    def test_fail_header_raises_exception(self, mock_pipe, mock_fork):
        """Test that fail header raises RestoreException with error message."""
        r_fd, w_fd = 3, 4
        mock_pipe.return_value = (r_fd, w_fd)
        mock_fork.return_value = 99999

        mock_fileobj = MagicMock()
        mock_fileobj.readline.side_effect = [b'fail\n', b'rdiff-backup crashed\n']

        with patch('os.fdopen', return_value=mock_fileobj):
            with patch('os.close'):
                with patch('rdiffweb.core.restore._wrap_close') as mock_wrap:
                    mock_wrap_instance = MagicMock()
                    mock_wrap.return_value = mock_wrap_instance

                    with self.assertRaises(RestoreException) as ctx:
                        pipe_restore(
                            b'/usr/bin/rdiff-backup',
                            b'/test/path',
                            1234567890,
                            'raw',
                            'utf-8',
                        )

                    self.assertIn('rdiff-backup crashed', str(ctx.exception))
                    mock_wrap_instance.close.assert_called_once()

    @patch('os.fork')
    @patch('os.pipe')
    def test_fail_header_without_message(self, mock_pipe, mock_fork):
        """Test that fail header without message still raises meaningful exception."""
        r_fd, w_fd = 3, 4
        mock_pipe.return_value = (r_fd, w_fd)
        mock_fork.return_value = 99999

        mock_fileobj = MagicMock()
        mock_fileobj.readline.side_effect = [b'fail\n', b'']

        with patch('os.fdopen', return_value=mock_fileobj):
            with patch('os.close'):
                with patch('rdiffweb.core.restore._wrap_close') as mock_wrap:
                    mock_wrap_instance = MagicMock()
                    mock_wrap.return_value = mock_wrap_instance

                    with self.assertRaises(RestoreException) as ctx:
                        pipe_restore(
                            b'/usr/bin/rdiff-backup',
                            b'/test/path',
                            1234567890,
                            'raw',
                            'utf-8',
                        )

                    self.assertIn('restore failed to start', str(ctx.exception))

    @patch('os.fork')
    @patch('os.pipe')
    def test_callbacks_passed_to_wrapper(self, mock_pipe, mock_fork):
        """Test that success/failure callbacks are passed to _wrap_close."""
        r_fd, w_fd = 3, 4
        mock_pipe.return_value = (r_fd, w_fd)
        mock_fork.return_value = 99999

        mock_fileobj = MagicMock()
        mock_fileobj.readline.side_effect = [b'ok\n', b'']

        def sc(): pass
        def fc(ec): pass

        with patch('os.fdopen', return_value=mock_fileobj):
            with patch('os.close'):
                with patch('rdiffweb.core.restore._wrap_close') as mock_wrap:
                    mock_wrap_instance = MagicMock()
                    mock_wrap.return_value = mock_wrap_instance

                    pipe_restore(
                        b'/usr/bin/rdiff-backup',
                        b'/test/path',
                        1234567890,
                        'raw',
                        'utf-8',
                        success_callback=sc,
                        failure_callback=fc,
                    )

                    mock_wrap.assert_called_once()
                    call_kwargs = mock_wrap.call_args
                    self.assertEqual(call_kwargs.kwargs['success_callback'], sc)
                    self.assertEqual(call_kwargs.kwargs['failure_callback'], fc)


class TestPathHandling(unittest.TestCase):
    """Test path quoting/unquoting and encoding handling."""

    def test_unquote_basic(self):
        """Test basic unquote functionality."""
        self.assertEqual(unquote(b'Char ;090 to quote'), b'Char Z to quote')

    def test_unquote_idempotent(self):
        """Test that unquote on already-unquoted path is safe."""
        self.assertEqual(unquote(b'Char Z to quote'), b'Char Z to quote')

    def test_unquote_multiple(self):
        """Test multiple quoted characters in path."""
        self.assertEqual(unquote(b';065;066;067'), b'ABC')

    def test_unquote_with_slash(self):
        """Test unquote with path separators."""
        self.assertEqual(
            unquote(b'Char ;090 to quote/Data'),
            b'Char Z to quote/Data'
        )

    def test_unquote_invalid_pattern(self):
        """Test that invalid patterns are left unchanged."""
        self.assertEqual(unquote(b';0ab'), b';0ab')
        self.assertEqual(unquote(b';999'), b';999')
        self.assertEqual(unquote(b';256'), b';256')
        self.assertEqual(unquote(b';255'), b'\xff')

    def test_non_utf8_bytes(self):
        """Test handling of non-UTF-8 byte sequences."""
        non_utf8 = b'file_\xff\xff_name.txt'
        self.assertEqual(unquote(non_utf8), non_utf8)

    def test_quoted_non_utf8(self):
        """Test quoted non-UTF-8 characters."""
        self.assertEqual(unquote(b'file_;255;255_name.txt'), b'file_\xff\xff_name.txt')


class TestFileGenerator(unittest.TestCase):
    """Test _file_generator stream handling and cleanup."""

    def setUp(self):
        import os
        os.environ['RDIFFWEB_TEST_DATABASE_URI'] = 'sqlite:///:memory:'
        import cherrypy
        from rdiffweb.rdw_app import RdiffwebApp
        cfg = RdiffwebApp.parse_args(args=[], config_file_contents='rate-limit=-1')
        RdiffwebApp(cfg)
        from rdiffweb.controller.page_restore import _file_generator
        self._file_generator = _file_generator

    def test_normal_iteration_closes_stream(self):
        """Test that stream is closed after normal iteration."""
        data = b'chunk1chunk2chunk3'
        stream = io.BytesIO(data)

        gen = self._file_generator(stream, chunkSize=6)
        chunks = list(gen)

        self.assertEqual(b''.join(chunks), data)
        self.assertTrue(gen._closed)
        self.assertTrue(stream.closed)

    def test_close_idempotent(self):
        """Test that multiple close() calls are safe."""
        stream = io.BytesIO(b'data')
        gen = self._file_generator(stream)

        gen.close()
        gen.close()
        gen.close()

        self.assertTrue(gen._closed)
        self.assertTrue(stream.closed)

    def test_exception_during_iteration_closes_stream(self):
        """Test that exception during iteration closes stream."""
        class BadStream(io.BytesIO):
            def read(self, size=-1):
                raise IOError('read failed')

        stream = BadStream(b'data')
        gen = self._file_generator(stream)

        with self.assertRaises(IOError):
            next(gen)

        self.assertTrue(gen._closed)
        self.assertTrue(stream.closed)

    def test_stopiteration_closes_stream(self):
        """Test that StopIteration closes stream properly."""
        stream = io.BytesIO(b'short')
        gen = self._file_generator(stream, chunkSize=100)

        chunks = list(gen)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], b'short')
        self.assertTrue(gen._closed)
        self.assertTrue(stream.closed)

    def test_del_fallback(self):
        """Test that __del__ closes stream if not already closed."""
        stream = io.BytesIO(b'data')
        gen = self._file_generator(stream)

        del gen
        import gc
        gc.collect()

        self.assertTrue(stream.closed)

    def test_close_on_aborted_iteration(self):
        """Test that closing generator mid-iteration closes stream."""
        stream = io.BytesIO(b'chunk1chunk2chunk3')
        gen = self._file_generator(stream, chunkSize=6)

        chunk1 = next(gen)
        self.assertEqual(chunk1, b'chunk1')
        self.assertFalse(gen._closed)

        gen.close()
        self.assertTrue(gen._closed)
        self.assertTrue(stream.closed)

    def test_iterate_after_close(self):
        """Test that iteration after close raises StopIteration."""
        stream = io.BytesIO(b'data')
        gen = self._file_generator(stream)

        gen.close()

        with self.assertRaises(StopIteration):
            next(gen)


class TestRepoObjectRestoreCallbacks(unittest.TestCase):
    """Test RepoObject.restore callback behavior and audit logging."""

    def setUp(self):
        import os
        os.environ['RDIFFWEB_TEST_DATABASE_URI'] = 'sqlite:///:memory:'
        import cherrypy
        from rdiffweb.rdw_app import RdiffwebApp
        cfg = RdiffwebApp.parse_args(args=[], config_file_contents='rate-limit=-1')
        self.app = RdiffwebApp(cfg)

    def test_restore_method_signature(self):
        """Test that RepoObject.restore accepts callback parameters."""
        from rdiffweb.core.model import RepoObject
        import inspect
        sig = inspect.signature(RepoObject.restore)
        params = list(sig.parameters.keys())
        self.assertIn('args', params)
        self.assertIn('kwargs', params)

    @patch('rdiffweb.core.model._repo.super')
    def test_callback_closure_captures_display_name(self, mock_super):
        """Test that callbacks correctly capture the display name."""
        from rdiffweb.core.model import RepoObject
        from rdiffweb.core.librdiff import unquote

        mock_repo = MagicMock(spec=RepoObject)
        mock_repo._decode = lambda b, errors='replace': b.decode('utf-8', errors)
        mock_repo.add_message = MagicMock()
        mock_repo.commit = MagicMock()

        test_path = b'test;090path'
        expected_display_name = 'testZpath'

        mock_super.return_value.restore = MagicMock(return_value=('filename', MagicMock()))

        result = RepoObject.restore(mock_repo, test_path)

        self.assertEqual(result[0], 'filename')

        call_kwargs = mock_super.return_value.restore.call_args.kwargs
        self.assertIn('success_callback', call_kwargs)
        self.assertIn('failure_callback', call_kwargs)

        success_cb = call_kwargs['success_callback']
        failure_cb = call_kwargs['failure_callback']

        success_cb()

        mock_repo.add_message.assert_called_once()
        message_call = mock_repo.add_message.call_args[0][0]
        self.assertIn(expected_display_name, message_call.body)

        mock_repo.add_message.reset_mock()

        failure_cb(42)
        self.assertEqual(mock_repo.add_message.call_count, 1)
        failure_msg = mock_repo.add_message.call_args[0][0]
        self.assertIn(expected_display_name, failure_msg.body)
        self.assertIn('42', failure_msg.body)

    @patch('rdiffweb.core.model._repo.super')
    def test_failure_callback_with_none_exit_code(self, mock_super):
        """Test failure callback when exit_code is None (exception case)."""
        from rdiffweb.core.model import RepoObject

        mock_repo = MagicMock(spec=RepoObject)
        mock_repo._decode = lambda b, errors='replace': b.decode('utf-8', errors)
        mock_repo.add_message = MagicMock()
        mock_repo.commit = MagicMock()

        mock_super.return_value.restore = MagicMock(return_value=('filename', MagicMock()))

        RepoObject.restore(mock_repo, b'testpath')

        call_kwargs = mock_super.return_value.restore.call_args.kwargs
        failure_cb = call_kwargs['failure_callback']

        failure_cb(None)

        mock_repo.add_message.assert_called_once()
        failure_msg = mock_repo.add_message.call_args[0][0]
        self.assertIn('failed', failure_msg.body)
        self.assertNotIn('exit code', failure_msg.body)

    @patch('rdiffweb.core.model._repo.super')
    def test_callback_exception_does_not_propagate(self, mock_super):
        """Test that callback exceptions are caught and logged, not propagated."""
        from rdiffweb.core.model import RepoObject

        mock_repo = MagicMock(spec=RepoObject)
        mock_repo._decode = lambda b, errors='replace': b.decode('utf-8', errors)
        mock_repo.add_message = MagicMock(side_effect=RuntimeError('db down'))
        mock_repo.commit = MagicMock()

        mock_super.return_value.restore = MagicMock(return_value=('filename', MagicMock()))

        RepoObject.restore(mock_repo, b'testpath')

        call_kwargs = mock_super.return_value.restore.call_args.kwargs
        success_cb = call_kwargs['success_callback']

        try:
            success_cb()
        except Exception as e:
            self.fail(f'success_cb should not propagate exceptions: {e}')


class TestSecurityPathValidation(unittest.TestCase):
    """Test that both raw and archive downloads use the same path validation."""

    def test_fstat_validates_path_for_raw(self):
        """Test that fstat is called for raw downloads to validate path."""
        from rdiffweb.core.librdiff import RdiffRepo

        with patch.object(RdiffRepo, 'fstat') as mock_fstat:
            mock_path_obj = MagicMock()
            mock_path_obj.isdir = False
            mock_path_obj.display_name = 'testfile.txt'
            mock_path_obj.path = b'testfile.txt'
            mock_fstat.return_value = mock_path_obj

            with patch('rdiffweb.core.librdiff.find_rdiff_backup', return_value=b'/usr/bin/rdiff-backup'):
                with patch('rdiffweb.core.librdiff.pipe_restore') as mock_pipe:
                    mock_pipe.return_value = MagicMock()

                    repo = RdiffRepo('/tmp/testrepo', 'utf-8')
                    repo.restore(b'testfile.txt', 1234567890, kind='raw')

                    mock_fstat.assert_called_once_with(b'testfile.txt')

    def test_fstat_validates_path_for_zip(self):
        """Test that fstat is called for zip downloads to validate path."""
        from rdiffweb.core.librdiff import RdiffRepo

        with patch.object(RdiffRepo, 'fstat') as mock_fstat:
            mock_path_obj = MagicMock()
            mock_path_obj.isdir = True
            mock_path_obj.display_name = 'testdir'
            mock_path_obj.path = b'testdir'
            mock_fstat.return_value = mock_path_obj

            with patch('rdiffweb.core.librdiff.find_rdiff_backup', return_value=b'/usr/bin/rdiff-backup'):
                with patch('rdiffweb.core.librdiff.pipe_restore') as mock_pipe:
                    mock_pipe.return_value = MagicMock()

                    repo = RdiffRepo('/tmp/testrepo', 'utf-8')
                    repo.restore(b'testdir', 1234567890, kind='zip')

                    mock_fstat.assert_called_once_with(b'testdir')

    def test_fstat_validates_path_for_tar_gz(self):
        """Test that fstat is called for tar.gz downloads."""
        from rdiffweb.core.librdiff import RdiffRepo

        with patch.object(RdiffRepo, 'fstat') as mock_fstat:
            mock_path_obj = MagicMock()
            mock_path_obj.isdir = True
            mock_path_obj.display_name = 'testdir'
            mock_path_obj.path = b'testdir'
            mock_fstat.return_value = mock_path_obj

            with patch('rdiffweb.core.librdiff.find_rdiff_backup', return_value=b'/usr/bin/rdiff-backup'):
                with patch('rdiffweb.core.librdiff.pipe_restore') as mock_pipe:
                    mock_pipe.return_value = MagicMock()

                    repo = RdiffRepo('/tmp/testrepo', 'utf-8')
                    repo.restore(b'testdir', 1234567890, kind='tar.gz')

                    mock_fstat.assert_called_once_with(b'testdir')

    def test_raw_on_directory_rejected(self):
        """Test that raw kind on directory raises ValueError."""
        from rdiffweb.core.librdiff import RdiffRepo

        with patch.object(RdiffRepo, 'fstat') as mock_fstat:
            mock_path_obj = MagicMock()
            mock_path_obj.isdir = True
            mock_fstat.return_value = mock_path_obj

            repo = RdiffRepo('/tmp/testrepo', 'utf-8')

            with self.assertRaises(ValueError) as ctx:
                repo.restore(b'testdir', 1234567890, kind='raw')

            self.assertIn('raw type not supported for directory', str(ctx.exception))

    def test_path_contains_unquote_call(self):
        """Test that path is unquoted before passing to rdiff-backup."""
        from rdiffweb.core.librdiff import RdiffRepo, unquote

        with patch.object(RdiffRepo, 'fstat') as mock_fstat:
            mock_path_obj = MagicMock()
            mock_path_obj.isdir = False
            mock_path_obj.display_name = 'test Z file'
            mock_path_obj.path = b'test ;090 file'
            mock_fstat.return_value = mock_path_obj

            with patch('rdiffweb.core.librdiff.find_rdiff_backup', return_value=b'/usr/bin/rdiff-backup'):
                with patch('rdiffweb.core.librdiff.pipe_restore') as mock_pipe:
                    mock_pipe.return_value = MagicMock()

                    repo = RdiffRepo('/tmp/testrepo', 'utf-8')
                    repo.restore(b'test ;090 file', 1234567890, kind='raw')

                    call_kwargs = mock_pipe.call_args.kwargs
                    self.assertEqual(
                        call_kwargs['path'],
                        os.path.join(b'/tmp/testrepo', unquote(b'test ;090 file'))
                    )


if __name__ == '__main__':
    unittest.main()
