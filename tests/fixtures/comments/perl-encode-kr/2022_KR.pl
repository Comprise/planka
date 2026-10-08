# источник: Perl 5.42.2, cpan/Encode/lib/Encode/KR/2022_KR.pm, строки 42–57 (вторая часть s{…}{…}geox на следующей строке)

sub iso_euc {
    my $r_str = shift;
    $$r_str =~ s/$RE{'2022_KR'}//gox;    # remove the designator
    $$r_str =~ s{                      # replace characters in GL
     \x0e                              # between SO(\x0e) and SI(\x0f)
     ([^\x0f]*)                        # with characters in GR
     \x0f
        }
    {
                        my $out= $1;
      $out =~ tr/\x21-\x7e/\xa1-\xfe/;
      $out;
    }geox;
    my ($residue) = ( $$r_str =~ s/(\e.*)$//so );
    return $residue;
