# источник: OpenSSH 10.6p1, contrib/findssl.sh, строки 59–107
#
# Adjust these to suit your compiler.
# You may also need to set the *LIB*PATH environment variables if
# DEFAULT_LIBPATH is not correct for your system.
#
CC=gcc
STATIC=-static

#
# Cleanup on interrupt
#
trap 'rm -f conftest.c' INT HUP TERM

#
# Set up conftest C source
#
rm -f findssl.log
cat >conftest.c <<EOD
#include <stdio.h>
int main(){printf("0x%08xL\n", SSLeay());}
EOD

#
# Set default library paths if not already set
#
DEFAULT_LIBPATH=/usr/lib:/usr/local/lib
LIBPATH=${LIBPATH:=$DEFAULT_LIBPATH}
LD_LIBRARY_PATH=${LD_LIBRARY_PATH:=$DEFAULT_LIBPATH}
LIBRARY_PATH=${LIBRARY_PATH:=$DEFAULT_LIBPATH}
export LIBPATH LD_LIBRARY_PATH LIBRARY_PATH

# not all platforms have a 'which' command
if which ls >/dev/null 2>/dev/null; then
    : which is defined
else
    which () {
	saveIFS="$IFS"
	IFS=:
	for p in $PATH; do
	    if test -x "$p/$1" -a -f "$p/$1"; then
		IFS="$saveIFS"
		echo "$p/$1"
		return 0
	    fi
	done
	IFS="$saveIFS"
	return 1
    }
fi
