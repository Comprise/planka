# источник: Perl 5.42.2, cpan/Module-Load/lib/Module/Load.pm, строки 130–136 (регулярки за return)

sub _is_file {
    local $_ = shift;
    return  /^\./               ? 1 :
            /[^\w:']/           ? 1 :
            undef
    #' silly bbedit..
