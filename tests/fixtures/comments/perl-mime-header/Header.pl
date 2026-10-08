# источник: Perl 5.42.2, cpan/Encode/lib/Encode/MIME/Header.pm, строки 105–181 (вложенные s{…}{…}se)

        # process sequence of encoded MIME words at once
        1 while not $stop and $line =~ s{^(.*?)$re_match_decode}{

            my $begin = $1 . $2;
            my $words = $3;

            $begin =~ tr/\r\n//d;
            $output .= $begin;

            # decode one MIME word
            1 while not $stop and $words =~ s{^(.*?)($re_capture_decode)}{

                $output .= $1;
                my $orig = $2;
                my $charset = $3;
                my ($mime_enc, $text) = split /\?/, $5;

                $text =~ tr/\r\n//d;

                my $enc = Encode::find_mime_encoding($charset);

                # in non strict mode allow also perl encoding aliases
                if ( not defined $enc and not $STRICT_DECODE ) {
                    # make sure that decoded string will be always strict UTF-8
                    $charset = 'UTF-8' if lc($charset) eq 'utf8';
                    $enc = Encode::find_encoding($charset);
                }

                if ( not defined $enc ) {
                    Carp::croak qq(Unknown charset "$charset") if not ref $chk and $chk and $chk & Encode::DIE_ON_ERR;
                    Carp::carp qq(Unknown charset "$charset") if not ref $chk and $chk and $chk & Encode::WARN_ON_ERR;
                    $stop = 1 if not ref $chk and $chk and $chk & Encode::RETURN_ON_ERR;
                    $output .= ($output =~ /(?:\A|[ \t])$/ ? '' : ' ') . $orig unless $stop; # $orig mime word is separated by whitespace
                    $stop ? $orig : '';
                } else {
                    if ( uc($mime_enc) eq 'B' and $obj->{decode_b} ) {
                        my $decoded = _decode_b($enc, $text, $chk);
                        $stop = 1 if not defined $decoded and not ref $chk and $chk and $chk & Encode::RETURN_ON_ERR;
                        $output .= (defined $decoded ? $decoded : $text) unless $stop;
                        $stop ? $orig : '';
                    } elsif ( uc($mime_enc) eq 'Q' and $obj->{decode_q} ) {
                        my $decoded = _decode_q($enc, $text, $chk);
                        $stop = 1 if not defined $decoded and not ref $chk and $chk and $chk & Encode::RETURN_ON_ERR;
                        $output .= (defined $decoded ? $decoded : $text) unless $stop;
                        $stop ? $orig : '';
                    } else {
                        Carp::croak qq(MIME "$mime_enc" unsupported) if not ref $chk and $chk and $chk & Encode::DIE_ON_ERR;
                        Carp::carp qq(MIME "$mime_enc" unsupported) if not ref $chk and $chk and $chk & Encode::WARN_ON_ERR;
                        $stop = 1 if not ref $chk and $chk and $chk & Encode::RETURN_ON_ERR;
                        $output .= ($output =~ /(?:\A|[ \t])$/ ? '' : ' ') . $orig unless $stop; # $orig mime word is separated by whitespace
                        $stop ? $orig : '';
                    }
                }

            }se;

            if ( not $stop ) {
                $output .= $words;
                $words = '';
            }

            $words;

        }se;

        if ( not $stop ) {
            $line =~ tr/\r\n//d;
            $output .= $line . $sep;
            $line = '';
            $sep = '';
        }

        $line . $sep;

    }se;

