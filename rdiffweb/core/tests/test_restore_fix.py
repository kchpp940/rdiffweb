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
import unittest
from unittest.mock import MagicMock, patch

from rdiffweb.core.restore import (
    _wrap_close,
    RestoreException,
    pipe_restore,
    RestoreState,
    STATE_INIT,
    STATE_PATH_VALIDATED,
    STATE_STREAM_ESTABLISHED,
    STATE_TRANSFER_COMPLETE,
    STATE_SUCCESS,
    STATE_FAILED,
)
from rdiffweb.core.librdiff import unquote


class TestRestoreState(unittest.TestCase):
    """Test RestoreState state machine transitions."""

    def setUp(self):
        self.success_count = 0
        self.failure_count = 0
        self.failure_args = []

    def success_cb(self):
        self.success_count += 1

    def failure_cb(self, exit_code, abort_reason=None):
        self.failure_count += 1
        self.failure_args.append((exit_code, abort_reason))

    def _create_state(self):
        return RestoreState(self.success_cb, self.failure_cb)

    def test_initial_state(self):
        """Test initial state."""
        state = self._create_state()
        self.assertEqual(state.state, STATE_INIT)
        self.assertFalse(state.is_success)
        self.assertFalse(state.is_failed)
        self.assertIsNone(state.return_code)
        self.assertIsNone(state.abort_reason)

    def test_phase1_path_validated(self):
        """Test phase 1: path validation."""
        state = self._create_state()
        state.mark_path_validated()
        self.assertEqual(state.state, STATE_PATH_VALIDATED)
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 0)

    def test_phase2_stream_established(self):
        """Test phase 2: stream established does NOT trigger callback."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        self.assertEqual(state.state, STATE_STREAM_ESTABLISHED)
        self.assertEqual(self.success_count, 0, 'callback should NOT fire on stream establishment')
        self.assertEqual(self.failure_count, 0)

    def test_phase3_transfer_complete_no_exit_yet(self):
        """Test phase 3: transfer complete but process still running."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_transfer_complete()
        self.assertEqual(state.state, STATE_TRANSFER_COMPLETE)
        self.assertEqual(self.success_count, 0, 'callback should NOT fire until process exits')
        self.assertEqual(self.failure_count, 0)

    def test_full_success_flow_transfer_first(self):
        """Test full success: transfer complete, then process exit 0."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_transfer_complete()
        state.mark_process_exited(0)

        self.assertEqual(state.state, STATE_SUCCESS)
        self.assertTrue(state.is_success)
        self.assertFalse(state.is_failed)
        self.assertEqual(self.success_count, 1)
        self.assertEqual(self.failure_count, 0)

    def test_full_success_flow_exit_first(self):
        """Test full success: process exit 0 first, then transfer complete."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_process_exited(0)
        self.assertEqual(state.state, STATE_STREAM_ESTABLISHED, 'should wait for transfer complete')
        self.assertEqual(self.success_count, 0)

        state.mark_transfer_complete()
        self.assertEqual(state.state, STATE_SUCCESS)
        self.assertEqual(self.success_count, 1)

    def test_failure_nonzero_exit_after_transfer(self):
        """Test failure: transfer complete but process exits non-zero."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_transfer_complete()
        state.mark_process_exited(1)

        self.assertEqual(state.state, STATE_FAILED)
        self.assertTrue(state.is_failed)
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 1)
        self.assertEqual(self.failure_args[0], (1, 'rdiff-backup exited with code 1'))

    def test_failure_nonzero_exit_before_transfer(self):
        """Test failure: process exits non-zero before transfer completes."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_process_exited(42)

        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 1)
        self.assertEqual(self.failure_args[0], (42, 'rdiff-backup exited with code 42'))

    def test_abort_during_transfer(self):
        """Test abort during transfer (e.g., browser disconnect)."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        state.abort('browser disconnected')

        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(state.abort_reason, 'browser disconnected')
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 1)
        self.assertEqual(self.failure_args[0], (None, 'browser disconnected'))

    def test_abort_before_stream_established(self):
        """Test abort before stream establishment (e.g., header check failed)."""
        state = self._create_state()
        state.mark_path_validated()
        state.abort('restore failed to start')

        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(state.abort_reason, 'restore failed to start')
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 1)

    def test_abort_idempotent(self):
        """Test that multiple abort() calls only trigger callback once."""
        state = self._create_state()
        state.mark_path_validated()
        state.abort('reason1')
        state.abort('reason2')
        state.abort('reason3')

        self.assertEqual(self.failure_count, 1)
        self.assertEqual(self.failure_args[0][1], 'reason1')

    def test_success_callback_not_called_on_stream_establish(self):
        """CRITICAL: verify success callback never fires on stream establishment alone."""
        state = self._create_state()
        state.mark_path_validated()

        for _ in range(10):
            state.mark_stream_established()

        self.assertEqual(self.success_count, 0, 'CRITICAL: success callback must NOT fire on stream establish!')
        self.assertEqual(state.state, STATE_STREAM_ESTABLISHED)

    def test_callback_invoked_only_once(self):
        """Test that callbacks are never called more than once."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_transfer_complete()
        state.mark_process_exited(0)

        # Try to trigger success again
        state.mark_process_exited(0)
        state.mark_transfer_complete()

        self.assertEqual(self.success_count, 1)
        self.assertEqual(self.failure_count, 0)

    def test_failure_does_not_trigger_success(self):
        """Test that failure state prevents success callback."""
        state = self._create_state()
        state.mark_path_validated()
        state.mark_stream_established()
        state.abort('failed')

        # Even if we try to mark success now, it should be ignored
        state.mark_transfer_complete()
        state.mark_process_exited(0)

        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 1)

    def test_mark_process_exited_on_failed_state(self):
        """Test that mark_process_exited on failed state still calls failure callback once."""
        state = self._create_state()
        state.mark_path_validated()
        state.abort('failed early')

        # mark_process_exited on failed state should not trigger second callback
        state.mark_process_exited(1)

        self.assertEqual(self.failure_count, 1)

    def test_callback_exception_does_not_propagate(self):
        """Test that callback exceptions are caught and logged, not propagated."""
        def bad_success_cb():
            raise RuntimeError('callback failed')

        state = RestoreState(bad_success_cb, self.failure_cb)
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_transfer_complete()

        try:
            state.mark_process_exited(0)
        except Exception as e:
            self.fail(f'callback exception should not propagate: {e}')

        self.assertEqual(state.state, STATE_SUCCESS)

    def test_failure_callback_exception_does_not_propagate(self):
        """Test that failure callback exceptions are caught."""
        def bad_failure_cb(exit_code, abort_reason=None):
            raise RuntimeError('failure callback failed')

        state = RestoreState(self.success_cb, bad_failure_cb)
        state.mark_path_validated()

        try:
            state.abort('test failure')
        except Exception as e:
            self.fail(f'failure callback exception should not propagate: {e}')

        self.assertEqual(state.state, STATE_FAILED)


class TestWrapCloseIntegration(unittest.TestCase):
    """Test _wrap_close integration with RestoreState."""

    def setUp(self):
        self.success_count = 0
        self.failure_count = 0
        self.failure_args = []

    def success_cb(self):
        self.success_count += 1

    def failure_cb(self, exit_code, abort_reason=None):
        self.failure_count += 1
        self.failure_args.append((exit_code, abort_reason))

    def _create_wrapper(self, pid=12345):
        state = RestoreState(self.success_cb, self.failure_cb)
        state.mark_path_validated()
        mock_stream = MagicMock()
        return _wrap_close(mock_stream, pid, state), mock_stream, state

    @patch('os.waitpid')
    @patch('os.kill')
    def test_full_success_flow(self, mock_kill, mock_waitpid):
        """Test full success: stream established, transfer complete, exit 0."""
        wrapper, stream, state = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper._set_stream_established()
        self.assertEqual(state.state, STATE_STREAM_ESTABLISHED)
        self.assertEqual(self.success_count, 0)

        wrapper.mark_transfer_complete()
        self.assertEqual(state.state, STATE_TRANSFER_COMPLETE)
        self.assertEqual(self.success_count, 0)

        wrapper.close()
        self.assertEqual(state.state, STATE_SUCCESS)
        self.assertEqual(self.success_count, 1)
        self.assertEqual(self.failure_count, 0)

    @patch('os.waitpid')
    @patch('os.kill')
    def test_abort_mid_transfer(self, mock_kill, mock_waitpid):
        """Test abort during transfer (browser disconnect)."""
        wrapper, stream, state = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper._set_stream_established()

        wrapper.abort('browser disconnected')

        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(state.abort_reason, 'browser disconnected')
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 1)
        self.assertEqual(self.failure_args[0], (None, 'browser disconnected'))

    @patch('os.waitpid')
    @patch('os.kill')
    def test_abort_does_not_call_close(self, mock_kill, mock_waitpid):
        """Test that abort() only updates state, does NOT call close()."""
        wrapper, stream, state = self._create_wrapper()

        wrapper._set_stream_established()
        wrapper.abort('browser disconnected')

        self.assertTrue(state.is_failed)
        self.assertFalse(wrapper._closed, 'abort() should NOT call close() - caller is responsible')

    @patch('os.waitpid')
    @patch('os.kill')
    def test_abort_then_close(self, mock_kill, mock_waitpid):
        """Test that abort() + close() together properly reap the process."""
        wrapper, stream, state = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper._set_stream_established()
        wrapper.abort('browser disconnected')
        wrapper.close()

        self.assertTrue(wrapper._closed)
        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(self.failure_count, 1)

    @patch('os.waitpid')
    @patch('os.kill')
    def test_nonzero_exit_after_transfer(self, mock_kill, mock_waitpid):
        """Test non-zero exit after successful transfer."""
        wrapper, stream, state = self._create_wrapper()
        mock_waitpid.return_value = (12345, 1 << 8)

        wrapper._set_stream_established()
        wrapper.mark_transfer_complete()
        wrapper.close()

        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 1)
        self.assertEqual(self.failure_args[0], (1, 'rdiff-backup exited with code 1'))

    @patch('os.waitpid')
    @patch('os.kill')
    def test_close_without_transfer_complete(self, mock_kill, mock_waitpid):
        """Test close() called before transfer complete (aborted transfer)."""
        wrapper, stream, state = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper._set_stream_established()
        # Don't call mark_transfer_complete - simulate incomplete transfer
        wrapper.close()

        # Should not be success because transfer wasn't marked complete
        self.assertNotEqual(state.state, STATE_SUCCESS)
        self.assertEqual(self.success_count, 0)

    @patch('os.waitpid')
    @patch('os.kill')
    def test_stream_established_only_not_success(self, mock_kill, mock_waitpid):
        """CRITICAL: stream established + exit 0 without transfer complete is NOT success."""
        wrapper, stream, state = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper._set_stream_established()
        wrapper.close()

        self.assertNotEqual(
            state.state,
            STATE_SUCCESS,
            'CRITICAL: stream established + exit 0 without transfer complete MUST NOT be success!',
        )
        self.assertEqual(
            self.success_count,
            0,
            'CRITICAL: success callback must NOT fire without transfer complete!',
        )

    @patch('os.waitpid')
    @patch('os.kill')
    def test_process_already_exited_with_transfer_complete(self, mock_kill, mock_waitpid):
        """Test ChildProcessError when transfer was complete."""
        wrapper, stream, state = self._create_wrapper()
        from errno import ECHILD
        mock_waitpid.side_effect = ChildProcessError(ECHILD, 'No child processes')

        wrapper._set_stream_established()
        wrapper.mark_transfer_complete()
        wrapper.close()

        # Should be marked success because transfer was complete
        self.assertEqual(state.state, STATE_SUCCESS)
        self.assertEqual(self.success_count, 1)

    @patch('os.waitpid')
    @patch('os.kill')
    def test_process_already_exited_without_transfer_complete(self, mock_kill, mock_waitpid):
        """Test ChildProcessError when transfer was NOT complete."""
        wrapper, stream, state = self._create_wrapper()
        from errno import ECHILD
        mock_waitpid.side_effect = ChildProcessError(ECHILD, 'No child processes')

        wrapper._set_stream_established()
        # No mark_transfer_complete()
        wrapper.close()

        # Should NOT be success - transfer was incomplete
        self.assertNotEqual(state.state, STATE_SUCCESS)
        self.assertEqual(self.success_count, 0)

    @patch('os.waitpid')
    @patch('os.kill')
    def test_abort_idempotent(self, mock_kill, mock_waitpid):
        """Test that multiple abort() calls don't trigger multiple callbacks."""
        wrapper, stream, state = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper._set_stream_established()
        wrapper.abort('reason1')
        wrapper.abort('reason2')
        wrapper.abort('reason3')

        self.assertEqual(self.failure_count, 1)
        self.assertEqual(self.failure_args[0][1], 'reason1')

    def test_close_idempotent(self):
        """Test that multiple close() calls are safe."""
        wrapper, stream, state = self._create_wrapper()

        wrapper.close()
        wrapper.close()
        wrapper.close()

        stream.close.assert_called_once()

    @patch('os.waitpid')
    @patch('os.kill')
    def test_context_manager_exception(self, mock_kill, mock_waitpid):
        """Test that exception in context triggers abort."""
        wrapper, stream, state = self._create_wrapper()
        mock_waitpid.return_value = (12345, 0 << 8)

        wrapper._set_stream_established()
        wrapper.mark_transfer_complete()

        try:
            with wrapper:
                raise ValueError('test error')
        except ValueError:
            pass

        # Exception should abort, even if transfer was complete
        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(self.success_count, 0)
        self.assertEqual(self.failure_count, 1)
        self.assertIn('test error', str(self.failure_args[0][1]))


class TestPipeRestoreHeader(unittest.TestCase):
    """Test pipe_restore header processing."""

    @patch('os.fork')
    @patch('os.pipe')
    def test_ok_header_marks_stream_established(self, mock_pipe, mock_fork):
        """Test that ok header advances state but does NOT trigger success callback."""
        r_fd, w_fd = 3, 4
        mock_pipe.return_value = (r_fd, w_fd)
        mock_fork.return_value = 99999

        mock_fileobj = MagicMock()
        mock_fileobj.readline.side_effect = [b'ok\n', b'']

        success_called = [False]

        def success_cb():
            success_called[0] = True

        state = RestoreState(success_cb, None)
        state.mark_path_validated()

        with patch('os.fdopen', return_value=mock_fileobj):
            with patch('os.close'):
                result = pipe_restore(
                    b'/usr/bin/rdiff-backup',
                    b'/test/path',
                    1234567890,
                    'raw',
                    'utf-8',
                    restore_state=state,
                )

                self.assertEqual(state.state, STATE_STREAM_ESTABLISHED)
                self.assertFalse(
                    success_called[0],
                    'CRITICAL: success callback must NOT fire on ok header!',
                )
                self.assertEqual(result.state, state)

    @patch('os.fork')
    @patch('os.pipe')
    def test_fail_header_triggers_abort(self, mock_pipe, mock_fork):
        """Test that fail header triggers abort with error message."""
        r_fd, w_fd = 3, 4
        mock_pipe.return_value = (r_fd, w_fd)
        mock_fork.return_value = 99999

        mock_fileobj = MagicMock()
        mock_fileobj.readline.side_effect = [b'fail\n', b'rdiff-backup crashed\n']

        failure_args = []

        def failure_cb(exit_code, abort_reason=None):
            failure_args.append((exit_code, abort_reason))

        state = RestoreState(None, failure_cb)
        state.mark_path_validated()

        with patch('os.fdopen', return_value=mock_fileobj):
            with patch('os.close'):
                with self.assertRaises(RestoreException) as ctx:
                    pipe_restore(
                        b'/usr/bin/rdiff-backup',
                        b'/test/path',
                        1234567890,
                        'raw',
                        'utf-8',
                        restore_state=state,
                    )

        self.assertIn('rdiff-backup crashed', str(ctx.exception))
        self.assertEqual(state.state, STATE_FAILED)
        self.assertEqual(state.abort_reason, 'rdiff-backup crashed')
        self.assertEqual(len(failure_args), 1)
        self.assertEqual(failure_args[0], (None, 'rdiff-backup crashed'))

    @patch('os.fork')
    @patch('os.pipe')
    def test_restore_state_passed_through(self, mock_pipe, mock_fork):
        """Test that RestoreState is passed through to wrapper."""
        r_fd, w_fd = 3, 4
        mock_pipe.return_value = (r_fd, w_fd)
        mock_fork.return_value = 99999

        mock_fileobj = MagicMock()
        mock_fileobj.readline.side_effect = [b'ok\n', b'']

        state = RestoreState()
        state.mark_path_validated()

        with patch('os.fdopen', return_value=mock_fileobj):
            with patch('os.close'):
                result = pipe_restore(
                    b'/usr/bin/rdiff-backup',
                    b'/test/path',
                    1234567890,
                    'raw',
                    'utf-8',
                    restore_state=state,
                )

                self.assertIs(result.state, state)


class TestFileGeneratorWithRestoreState(unittest.TestCase):
    """Test _file_generator integration with _wrap_close and RestoreState."""

    def setUp(self):
        import os
        os.environ['RDIFFWEB_TEST_DATABASE_URI'] = 'sqlite:///:memory:'
        from rdiffweb.rdw_app import RdiffwebApp
        cfg = RdiffwebApp.parse_args(args=[], config_file_contents='rate-limit=-1')
        RdiffwebApp(cfg)
        from rdiffweb.controller.page_restore import _file_generator
        self._file_generator = _file_generator

    def _create_mock_wrapper(self, data=b'', stream=None):
        """Create a mock _wrap_close with state management.

        Args:
            data: bytes data for the default BytesIO stream
            stream: optional custom stream to use instead of BytesIO
        """
        state = RestoreState()
        state.mark_path_validated()
        state.mark_stream_established()

        mock_stream = stream if stream is not None else io.BytesIO(data)

        class MockWrapClose:
            def __init__(self, stream, state):
                self._stream = stream
                self.state = state
                self._transfer_complete = False
                self._closed = False

            def read(self, size=-1):
                return self._stream.read(size)

            def mark_transfer_complete(self):
                self._transfer_complete = True
                self.state.mark_transfer_complete()

            def abort(self, reason=None):
                self.state.abort(reason)

            def close(self):
                if not self._closed:
                    self._closed = True
                    try:
                        self._stream.close()
                    except Exception:
                        pass

            def __getattr__(self, name):
                return getattr(self._stream, name)

        wrapper = MockWrapClose(mock_stream, state)
        return wrapper, state

    def test_transfer_complete_calls_mark_transfer_complete(self):
        """Test that successful transfer calls mark_transfer_complete()."""
        data = b'chunk1chunk2chunk3'
        wrapper, state = self._create_mock_wrapper(data)

        gen = self._file_generator(wrapper, chunkSize=6)
        chunks = list(gen)

        self.assertEqual(b''.join(chunks), data)
        self.assertTrue(wrapper._transfer_complete)
        self.assertEqual(state.state, STATE_TRANSFER_COMPLETE)

    def test_exception_during_transfer_calls_abort(self):
        """Test that exception during transfer calls abort()."""
        class BadStream:
            def read(self, size=-1):
                raise IOError('connection reset')

            def close(self):
                pass

        wrapper, state = self._create_mock_wrapper(stream=BadStream())

        gen = self._file_generator(wrapper)

        with self.assertRaises(IOError):
            next(gen)

        self.assertTrue(wrapper._closed)
        self.assertEqual(state.state, STATE_FAILED)
        self.assertIn('connection reset', state.abort_reason)

    def test_close_before_end_calls_abort(self):
        """Test that closing generator mid-transfer calls abort() then close()."""
        data = b'chunk1chunk2chunk3'
        wrapper, state = self._create_mock_wrapper(data)

        gen = self._file_generator(wrapper, chunkSize=6)
        chunk1 = next(gen)
        self.assertEqual(chunk1, b'chunk1')
        self.assertFalse(wrapper._transfer_complete)

        gen.close()

        self.assertTrue(wrapper._closed)
        self.assertEqual(state.state, STATE_FAILED)
        self.assertIn('aborted by client', state.abort_reason)

    def test_close_after_transfer_completes_normally(self):
        """Test that close() after transfer complete just calls input.close()."""
        data = b'chunk1chunk2chunk3'
        wrapper, state = self._create_mock_wrapper(data)

        gen = self._file_generator(wrapper, chunkSize=6)
        chunks = list(gen)

        self.assertEqual(b''.join(chunks), data)
        self.assertTrue(wrapper._transfer_complete)
        self.assertTrue(wrapper._closed)
        self.assertEqual(state.state, STATE_TRANSFER_COMPLETE)

    def test_del_fallback_calls_abort(self):
        """Test that __del__ on incomplete transfer calls abort()."""
        data = b'chunk1chunk2chunk3'
        wrapper, state = self._create_mock_wrapper(data)

        gen = self._file_generator(wrapper, chunkSize=6)
        next(gen)

        del gen
        import gc
        gc.collect()

        self.assertTrue(wrapper._closed)
        self.assertEqual(state.state, STATE_FAILED)

    def test_regular_file_without_state(self):
        """Test that regular files without state management still work."""
        data = b'test data'
        stream = io.BytesIO(data)

        gen = self._file_generator(stream, chunkSize=4)
        chunks = list(gen)

        self.assertEqual(b''.join(chunks), data)
        self.assertTrue(stream.closed)


class TestPathHandling(unittest.TestCase):
    """Test path quoting/unquoting and encoding handling."""

    def test_unquote_basic(self):
        self.assertEqual(unquote(b'Char ;090 to quote'), b'Char Z to quote')

    def test_unquote_idempotent(self):
        self.assertEqual(unquote(b'Char Z to quote'), b'Char Z to quote')

    def test_unquote_multiple(self):
        self.assertEqual(unquote(b';065;066;067'), b'ABC')

    def test_unquote_with_slash(self):
        self.assertEqual(
            unquote(b'Char ;090 to quote/Data'),
            b'Char Z to quote/Data'
        )

    def test_unquote_invalid_pattern(self):
        self.assertEqual(unquote(b';0ab'), b';0ab')
        self.assertEqual(unquote(b';999'), b';999')
        self.assertEqual(unquote(b';256'), b';256')
        self.assertEqual(unquote(b';255'), b'\xff')

    def test_non_utf8_bytes(self):
        non_utf8 = b'file_\xff\xff_name.txt'
        self.assertEqual(unquote(non_utf8), non_utf8)

    def test_quoted_non_utf8(self):
        self.assertEqual(unquote(b'file_;255;255_name.txt'), b'file_\xff\xff_name.txt')


class TestRepoObjectRestoreCallbacks(unittest.TestCase):
    """Test that RepoObject.restore creates a single lifecycle event."""

    def setUp(self):
        import os
        os.environ['RDIFFWEB_TEST_DATABASE_URI'] = 'sqlite:///:memory:'
        from rdiffweb.rdw_app import RdiffwebApp
        cfg = RdiffwebApp.parse_args(args=[], config_file_contents='rate-limit=-1')
        self.app = RdiffwebApp(cfg)

    def _setup_mocks(self, mock_super, mock_message_cls, pending_id=42):
        from rdiffweb.core.model import RepoObject

        mock_repo = MagicMock(spec=RepoObject)
        mock_repo._decode = lambda b, errors='replace': b.decode('utf-8', errors)
        mock_repo.add_message = MagicMock()
        mock_repo.commit = MagicMock()

        mock_pending = MagicMock()
        mock_pending.id = pending_id
        mock_pending.body = 'Restoring ...'
        mock_message_cls.return_value = mock_pending

        mock_query = MagicMock()
        mock_query.get = MagicMock(return_value=mock_pending)
        mock_message_cls.query = mock_query

        mock_super.return_value.restore = MagicMock(return_value=('filename', MagicMock()))

        return mock_repo, mock_pending

    @patch('rdiffweb.core.model._repo.Message')
    @patch('rdiffweb.core.model._repo.super')
    def test_pending_message_created_on_restore_start(self, mock_super, mock_message_cls):
        """Test that a pending message is created at the start of restore."""
        from rdiffweb.core.model import RepoObject

        mock_repo, mock_pending = self._setup_mocks(mock_super, mock_message_cls)

        RepoObject.restore(mock_repo, b'testfile.txt')

        # Pending message created with "Restoring ..." text
        self.assertTrue(mock_message_cls.called)
        first_kw = mock_message_cls.call_args_list[0][1]
        self.assertIn('Restoring', first_kw['body'])
        self.assertIn('testfile.txt', first_kw['body'])

        # add_message called once for pending
        self.assertEqual(mock_repo.add_message.call_count, 1)

        # commit called to persist pending
        self.assertTrue(mock_repo.commit.called)

        # super().restore called with callbacks
        call_kwargs = mock_super.return_value.restore.call_args.kwargs
        self.assertIn('success_callback', call_kwargs)
        self.assertIn('failure_callback', call_kwargs)

    @patch('rdiffweb.core.model._repo.Message')
    @patch('rdiffweb.core.model._repo.super')
    def test_success_callback_updates_pending_message(self, mock_super, mock_message_cls):
        """Test that success callback updates the pending message, not adding new."""
        from rdiffweb.core.model import RepoObject

        mock_repo, mock_pending = self._setup_mocks(mock_super, mock_message_cls)

        RepoObject.restore(mock_repo, b'testfile.txt')

        call_kwargs = mock_super.return_value.restore.call_args.kwargs
        success_cb = call_kwargs['success_callback']

        # Trigger success callback
        success_cb()

        # Pending message body was updated
        self.assertIn('succeeded', mock_pending.body)
        self.assertIn('testfile.txt', mock_pending.body)

        # No NEW message added (add_message only called once for pending)
        self.assertEqual(mock_repo.add_message.call_count, 1)

    @patch('rdiffweb.core.model._repo.Message')
    @patch('rdiffweb.core.model._repo.super')
    def test_failure_callback_updates_pending_message(self, mock_super, mock_message_cls):
        """Test that failure callback updates the pending message with reason."""
        from rdiffweb.core.model import RepoObject

        mock_repo, mock_pending = self._setup_mocks(mock_super, mock_message_cls)

        RepoObject.restore(mock_repo, b'testfile.txt')

        call_kwargs = mock_super.return_value.restore.call_args.kwargs
        failure_cb = call_kwargs['failure_callback']

        failure_cb(None, 'client disconnected')

        self.assertIn('failed', mock_pending.body)
        self.assertIn('client disconnected', mock_pending.body)
        self.assertEqual(mock_repo.add_message.call_count, 1)

    @patch('rdiffweb.core.model._repo.Message')
    @patch('rdiffweb.core.model._repo.super')
    def test_failure_callback_with_exit_code_updates_pending(self, mock_super, mock_message_cls):
        """Test failure callback with exit code also updates pending message."""
        from rdiffweb.core.model import RepoObject

        mock_repo, mock_pending = self._setup_mocks(mock_super, mock_message_cls)

        RepoObject.restore(mock_repo, b'testfile.txt')

        call_kwargs = mock_super.return_value.restore.call_args.kwargs
        failure_cb = call_kwargs['failure_callback']

        failure_cb(42, None)

        self.assertIn('failed', mock_pending.body)
        self.assertIn('exit code 42', mock_pending.body)
        self.assertEqual(mock_repo.add_message.call_count, 1)

    @patch('rdiffweb.core.model._repo.Message')
    @patch('rdiffweb.core.model._repo.super')
    def test_fallback_creates_new_message_if_pending_not_found(self, mock_super, mock_message_cls):
        """Test that if pending message can't be found, a new message is created."""
        from rdiffweb.core.model import RepoObject

        mock_repo, mock_pending = self._setup_mocks(mock_super, mock_message_cls)
        # Make query.get return None (pending not found)
        mock_message_cls.query.get.return_value = None

        RepoObject.restore(mock_repo, b'testfile.txt')

        call_kwargs = mock_super.return_value.restore.call_args.kwargs
        success_cb = call_kwargs['success_callback']

        success_cb()

        # add_message called twice: pending + fallback new message
        self.assertEqual(mock_repo.add_message.call_count, 2)

    @patch('rdiffweb.core.model._repo.Message')
    @patch('rdiffweb.core.model._repo.super')
    def test_callback_exception_does_not_propagate(self, mock_super, mock_message_cls):
        """Test that callback exceptions are caught and logged."""
        from rdiffweb.core.model import RepoObject

        mock_repo, mock_pending = self._setup_mocks(mock_super, mock_message_cls)
        # Make Message.query.get raise an exception during callback
        mock_message_cls.query.get = MagicMock(side_effect=RuntimeError('db down'))

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
        from rdiffweb.core.librdiff import RdiffRepo

        with patch.object(RdiffRepo, 'fstat') as mock_fstat:
            mock_path_obj = MagicMock()
            mock_path_obj.isdir = True
            mock_fstat.return_value = mock_path_obj

            repo = RdiffRepo('/tmp/testrepo', 'utf-8')

            with self.assertRaises(ValueError) as ctx:
                repo.restore(b'testdir', 1234567890, kind='raw')

            self.assertIn('raw type not supported for directory', str(ctx.exception))

    def test_restore_state_created_with_callbacks(self):
        """Test that RdiffRepo.restore creates RestoreState with callbacks."""
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

                    def sc():
                        pass

                    def fc(ec, ar=None):
                        pass

                    repo = RdiffRepo('/tmp/testrepo', 'utf-8')
                    repo.restore(
                        b'testfile.txt', 1234567890, kind='raw',
                        success_callback=sc, failure_callback=fc
                    )

                    call_kwargs = mock_pipe.call_args.kwargs
                    self.assertIn('restore_state', call_kwargs)
                    state = call_kwargs['restore_state']
                    from rdiffweb.core.restore import RestoreState
                    self.assertIsInstance(state, RestoreState)
                    self.assertEqual(state.state, STATE_PATH_VALIDATED)


class TestPathValidationFailure(unittest.TestCase):
    """Test that path validation failures trigger failure_callback."""

    def test_fstat_access_denied_triggers_failure_callback(self):
        from rdiffweb.core.librdiff import RdiffRepo, AccessDeniedError

        failure_args = []

        def failure_cb(exit_code, abort_reason=None):
            failure_args.append((exit_code, abort_reason))

        with patch.object(RdiffRepo, 'fstat', side_effect=AccessDeniedError('access denied')):
            repo = RdiffRepo('/tmp/testrepo', 'utf-8')

            with self.assertRaises(AccessDeniedError):
                repo.restore(
                    b'../etc/passwd', 1234567890, kind='raw',
                    failure_callback=failure_cb,
                )

        self.assertEqual(len(failure_args), 1)
        self.assertIn('access denied', str(failure_args[0][1]))

    def test_fstat_does_not_exist_triggers_failure_callback(self):
        from rdiffweb.core.librdiff import RdiffRepo, DoesNotExistError

        failure_args = []

        def failure_cb(exit_code, abort_reason=None):
            failure_args.append((exit_code, abort_reason))

        with patch.object(RdiffRepo, 'fstat', side_effect=DoesNotExistError(b'notfound')):
            repo = RdiffRepo('/tmp/testrepo', 'utf-8')

            with self.assertRaises(DoesNotExistError):
                repo.restore(
                    b'notfound', 1234567890, kind='raw',
                    failure_callback=failure_cb,
                )

        self.assertEqual(len(failure_args), 1)
        self.assertIn('notfound', str(failure_args[0][1]))

    def test_raw_on_directory_triggers_failure_callback(self):
        from rdiffweb.core.librdiff import RdiffRepo

        failure_args = []

        def failure_cb(exit_code, abort_reason=None):
            failure_args.append((exit_code, abort_reason))

        with patch.object(RdiffRepo, 'fstat') as mock_fstat:
            mock_path_obj = MagicMock()
            mock_path_obj.isdir = True
            mock_fstat.return_value = mock_path_obj

            repo = RdiffRepo('/tmp/testrepo', 'utf-8')

            with self.assertRaises(ValueError):
                repo.restore(
                    b'testdir', 1234567890, kind='raw',
                    failure_callback=failure_cb,
                )

        self.assertEqual(len(failure_args), 1)
        self.assertIn('raw type not supported', str(failure_args[0][1]))

    def test_fstat_failure_does_not_trigger_success_callback(self):
        from rdiffweb.core.librdiff import RdiffRepo, AccessDeniedError

        success_called = [False]

        def success_cb():
            success_called[0] = True

        failure_args = []

        def failure_cb(exit_code, abort_reason=None):
            failure_args.append((exit_code, abort_reason))

        with patch.object(RdiffRepo, 'fstat', side_effect=AccessDeniedError('denied')):
            repo = RdiffRepo('/tmp/testrepo', 'utf-8')

            with self.assertRaises(AccessDeniedError):
                repo.restore(
                    b'../etc/passwd', 1234567890, kind='raw',
                    success_callback=success_cb,
                    failure_callback=failure_cb,
                )

        self.assertFalse(success_called[0], 'success_callback must NOT fire on fstat failure')
        self.assertEqual(len(failure_args), 1)


class TestArchiveFailurePropagation(unittest.TestCase):
    """Test that archive creation failures are propagated to the parent process."""

    def test_archive_error_returns_custom_exit_code(self):
        from rdiffweb.core.restore import _restore, CUST_EXIT_CODE

        mock_rdiff = b'/usr/bin/rdiff-backup'
        mock_dest = MagicMock()

        with patch('rdiffweb.core.restore.subprocess.Popen') as mock_popen:
            mock_proc = MagicMock()
            mock_proc.stdout = iter([b'Processing changed file test.txt\n'])
            mock_proc.wait.return_value = 0
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            with patch('rdiffweb.core.restore._lookup_filename', return_value=(b'/tmp/test.txt', b'test.txt')):
                with patch('os.path.isdir', return_value=False):
                    with patch('os.path.isfile', return_value=True):
                        with patch('os.lstat') as mock_lstat:
                            mock_lstat.return_value = MagicMock(st_mode=0o100644)

                            with patch('rdiffweb.core.restore.ARCHIVERS', {'raw': MagicMock(side_effect=OSError('disk full'))}):
                                result = _restore(
                                    mock_rdiff,
                                    b'/test/path',
                                    1234567890,
                                    'raw',
                                    'utf-8',
                                    mock_dest,
                                    send_header=True,
                                )

                                self.assertEqual(result, CUST_EXIT_CODE, 'archive failure must return CUST_EXIT_CODE')


class TestCriticalStateTransitions(unittest.TestCase):
    """CRITICAL tests for state transitions that must never happen incorrectly."""

    def setUp(self):
        self.success_count = 0
        self.failure_count = 0

    def success_cb(self):
        self.success_count += 1

    def failure_cb(self, exit_code, abort_reason=None):
        self.failure_count += 1

    def test_stream_established_never_calls_success(self):
        """CRITICAL: mark_stream_established() MUST NEVER call success callback."""
        state = RestoreState(self.success_cb, self.failure_cb)
        state.mark_path_validated()

        for i in range(100):
            state.mark_stream_established()

        self.assertEqual(
            self.success_count,
            0,
            f'CRITICAL: success callback called {self.success_count} times on stream establish!',
        )
        self.assertEqual(self.failure_count, 0)

    def test_transfer_complete_without_process_exit_never_calls_success(self):
        """CRITICAL: mark_transfer_complete() without process exit MUST NEVER call success callback."""
        state = RestoreState(self.success_cb, self.failure_cb)
        state.mark_path_validated()
        state.mark_stream_established()

        for i in range(100):
            state.mark_transfer_complete()

        self.assertEqual(
            self.success_count,
            0,
            f'CRITICAL: success callback called {self.success_count} times on transfer complete without exit!',
        )
        self.assertEqual(self.failure_count, 0)

    def test_process_exit_without_transfer_complete_never_calls_success(self):
        """CRITICAL: mark_process_exited(0) without transfer complete MUST NEVER call success callback."""
        state = RestoreState(self.success_cb, self.failure_cb)
        state.mark_path_validated()
        state.mark_stream_established()

        for i in range(100):
            state.mark_process_exited(0)

        self.assertEqual(
            self.success_count,
            0,
            f'CRITICAL: success callback called {self.success_count} times on exit without transfer!',
        )
        self.assertEqual(self.failure_count, 0)

    def test_abort_never_calls_success(self):
        """CRITICAL: abort() MUST NEVER call success callback."""
        state = RestoreState(self.success_cb, self.failure_cb)
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_transfer_complete()

        state.abort('test abort')

        # Even if we try to mark success after abort
        state.mark_process_exited(0)

        self.assertEqual(
            self.success_count,
            0,
            'CRITICAL: success callback called after abort!',
        )
        self.assertEqual(self.failure_count, 1)

    def test_nonzero_exit_never_calls_success(self):
        """CRITICAL: mark_process_exited(non-zero) MUST NEVER call success callback."""
        state = RestoreState(self.success_cb, self.failure_cb)
        state.mark_path_validated()
        state.mark_stream_established()
        state.mark_transfer_complete()

        for exit_code in [1, 2, 65, 255]:
            self.success_count = 0
            state2 = RestoreState(self.success_cb, self.failure_cb)
            state2.mark_path_validated()
            state2.mark_stream_established()
            state2.mark_transfer_complete()
            state2.mark_process_exited(exit_code)

            self.assertEqual(
                self.success_count,
                0,
                f'CRITICAL: success callback called for exit code {exit_code}!',
            )

    def test_only_both_conditions_call_success_once(self):
        """Only mark_transfer_complete() + mark_process_exited(0) calls success exactly once."""
        state = RestoreState(self.success_cb, self.failure_cb)
        state.mark_path_validated()
        state.mark_stream_established()

        # Neither condition met
        self.assertEqual(self.success_count, 0)

        # First condition met
        state.mark_transfer_complete()
        self.assertEqual(self.success_count, 0)

        # Both conditions met
        state.mark_process_exited(0)
        self.assertEqual(self.success_count, 1)

        # Try to trigger again
        state.mark_process_exited(0)
        state.mark_transfer_complete()
        self.assertEqual(self.success_count, 1, 'callback should only be called once')


if __name__ == '__main__':
    unittest.main()
