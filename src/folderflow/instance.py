"""Windows instance locking, compatible with earlier portable releases."""
import errno
import os


class AlreadyRunning(Exception):
    pass


def acquire_instance_lock(path):
    import msvcrt
    handle = open(path,'a+b')
    try:
        handle.seek(0)
        try:
            # The first byte may already be locked by another instance. Never
            # read it before locking: Windows rejects reads of a locked region.
            # msvcrt supports locking beyond EOF, including a fresh empty file.
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError as error:
            if error.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):
                raise AlreadyRunning() from error
            raise
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
            handle.flush()
        return handle
    except BaseException:
        handle.close()
        raise
