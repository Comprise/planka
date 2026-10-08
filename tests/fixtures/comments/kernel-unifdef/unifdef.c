/* источник: Linux 7.2.9, scripts/unifdef.c, строки 59–80 и 1040–1084 */
const char copyright[] =
    "@(#) $Version: unifdef-2.5 $\n"
    "@(#) $Author: Tony Finch (dot@dotat.at) $\n"
    "@(#) $URL: http://dotat.at/prog/unifdef $\n"
;

/* types of input lines: */
typedef enum {
	LT_TRUEI,		/* a true #if with ignore flag */
	LT_FALSEI,		/* a false #if with ignore flag */
	LT_IF,			/* an unknown #if */
	LT_TRUE,		/* a true #if */
	LT_FALSE,		/* a false #if */
	LT_ELIF,		/* an unknown #elif */
	LT_ELTRUE,		/* a true #elif */
	LT_ELFALSE,		/* a false #elif */
	LT_ELSE,		/* #else */
	LT_ENDIF,		/* #endif */
	LT_DODGY,		/* flag: directive is not on one line */
	LT_DODGY_LAST = LT_DODGY + LT_ENDIF,
	LT_PLAIN,		/* ordinary line */
	LT_EOF,			/* end of file */
				cp += 1;
			continue;
		case C_COMMENT:
			if (strncmp(cp, "*\\\r\n", 4) == 0) {
				incomment = FINISHING_COMMENT;
				cp += 4;
			} else if (strncmp(cp, "*\\\n", 3) == 0) {
				incomment = FINISHING_COMMENT;
				cp += 3;
			} else if (strncmp(cp, "*/", 2) == 0) {
				incomment = NO_COMMENT;
				cp += 2;
			} else
				cp += 1;
			continue;
		case STARTING_COMMENT:
			if (*cp == '*') {
				incomment = C_COMMENT;
				cp += 1;
			} else if (*cp == '/') {
				incomment = CXX_COMMENT;
				cp += 1;
			} else {
				incomment = NO_COMMENT;
				linestate = LS_DIRTY;
			}
			continue;
		case FINISHING_COMMENT:
			if (*cp == '/') {
				incomment = NO_COMMENT;
				cp += 1;
			} else
				incomment = C_COMMENT;
			continue;
		default:
			abort(); /* bug */
		}
	return (cp);
}

/*
 * Skip macro arguments.
 */
static const char *
skipargs(const char *cp)
