# источник: Perl 5.42.2, ExtUtils/Constant/ProxySubs.pm, строки 455–534 (heredoc << "DONT")
    print $xs_fh <<"EOBOOT";
	if (C_ARRAY_LENGTH(values_for_notfound) > 1) {
#ifndef SYMBIAN
	    HV *const ${c_subname}_missing = get_missing_hash(aTHX);
#endif
	    const struct notfound_s *value_for_notfound = values_for_notfound;
	    do {
EOBOOT

    print $xs_fh $explosives ? <<"EXPLODE" : << "DONT";
		SV *tripwire = newSV(0);
		
		sv_magicext(tripwire, 0, PERL_MAGIC_ext, &not_defined_vtbl, 0, 0);
		SvPV_set(tripwire, (char *)value_for_notfound->name);
		if(value_for_notfound->namelen >= 0) {
		    SvCUR_set(tripwire, value_for_notfound->namelen);
	    	} else {
		    SvCUR_set(tripwire, -value_for_notfound->namelen);
		    SvUTF8_on(tripwire);
		}
		SvPOKp_on(tripwire);
		SvREADONLY_on(tripwire);
		assert(SvLEN(tripwire) == 0);

		$add_symbol_subname($athx symbol_table, value_for_notfound->name,
				    value_for_notfound->namelen, tripwire);
EXPLODE

		/* Need to add prototypes, else parsing will vary by platform.  */
		HE *he = (HE*) hv_common_key_len(symbol_table,
						 value_for_notfound->name,
						 value_for_notfound->namelen,
						 HV_FETCH_LVALUE, NULL, 0);
		SV *sv;
#ifndef SYMBIAN
		HEK *hek;
#endif
		if (!he) {
		    croak("Couldn't add key '%s' to %%$package_sprintf_safe\::",
			  value_for_notfound->name);
		}
		sv = HeVAL(he);
		if (!SvOK(sv) && SvTYPE(sv) != SVt_PVGV) {
		    /* Nothing was here before, so mark a prototype of ""  */
		    sv_setpvn(sv, "", 0);
		} else if (SvPOK(sv) && SvCUR(sv) == 0) {
		    /* There is already a prototype of "" - do nothing  */
		} else {
		    /* Someone has been here before us - have to make a real
		       typeglob.  */
		    /* It turns out to be incredibly hard to deal with all the
		       corner cases of sub foo (); and reporting errors correctly,
		       so lets cheat a bit.  Start with a constant subroutine  */
		    CV *cv = newCONSTSUB(symbol_table,
					 ${cast_CONSTSUB}value_for_notfound->name,
					 &PL_sv_yes);
		    /* and then turn it into a non constant declaration only.  */
		    SvREFCNT_dec(CvXSUBANY(cv).any_ptr);
		    CvCONST_off(cv);
		    CvXSUB(cv) = NULL;
		    CvXSUBANY(cv).any_ptr = NULL;
		}
#ifndef SYMBIAN
		hek = HeKEY_hek(he);
		if (!hv_common(${c_subname}_missing, NULL, HEK_KEY(hek),
 			       HEK_LEN(hek), HEK_FLAGS(hek), HV_FETCH_ISSTORE,
			       &PL_sv_yes, HEK_HASH(hek)))
		    croak("Couldn't add key '%s' to missing_hash",
			  value_for_notfound->name);
#endif
DONT

    print $xs_fh "		av_push(push, newSVhek(hek));\n"
	if $push;

    print $xs_fh <<"EOBOOT";
	    } while ((++value_for_notfound)->name);
	}
EOBOOT

