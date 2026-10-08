# источник: Perl 5.42.2, cpan/CPAN/lib/CPAN/Distribution.pm, строки 18–70 (split /=/)
my %instance;
for my $method (qw(get make test install)) {
    no strict 'refs';
    for my $prefix (qw(pre post)) {
        my $hookname = sprintf "%s_%s", $prefix, $method;
        *$hookname = sub {
            my($self) = @_;
            for my $plugin (@{$CPAN::Config->{plugin_list}}) {
                my($plugin_proper,$args) = split /=/, $plugin, 2;
                $args = "" unless defined $args;
                if ($CPAN::META->has_inst($plugin_proper)){
                    my @args = split /,/, $args;
                    $instance{$plugin} ||= $plugin_proper->new(@args);
                    if ($instance{$plugin}->can($hookname)) {
                        $instance{$plugin}->$hookname($self);
                    }
                } else {
                    $CPAN::Frontend->mydie("Plugin '$plugin_proper' not found for hook '$hookname'");
                }
            }
        };
    }
}

# Accessors
sub cpan_comment {
    my $self = shift;
    my $ro = $self->ro or return;
    $ro->{CPAN_COMMENT}
}

#-> CPAN::Distribution::undelay
sub undelay {
    my $self = shift;
    for my $delayer (
                     "configure_requires_later",
                     "configure_requires_later_for",
                     "later",
                     "later_for",
                    ) {
        delete $self->{$delayer};
    }
}

#-> CPAN::Distribution::is_dot_dist
sub is_dot_dist {
    my($self) = @_;
    return substr($self->id,-1,1) eq ".";
}

# add the A/AN/ stuff
#-> CPAN::Distribution::normalize
sub normalize {
