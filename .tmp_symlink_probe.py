
import os, tempfile, pathlib, sys
d = pathlib.Path(tempfile.mkdtemp())
tgt = d / 't.txt'; tgt.write_text('x')
try:
    lnk = d / 'l.txt'
    os.symlink(tgt, lnk)
    print('SYMLINK-FILE OK', lnk.is_symlink())
except Exception as e:
    print('SYMLINK FAIL', type(e).__name__, e)
sub = d / 'sub'; sub.mkdir()
try:
    dl = d / 'dlink'
    os.symlink(sub, dl, target_is_directory=True)
    print('SYMLINK-DIR OK')
except Exception as e:
    print('DIRLINK FAIL', type(e).__name__, e)
print(sys.version)
