#!/usr/bin/env perl
# Minimalist Perl example: clean subroutine encapsulation
use strict;
use warnings;

$| = 1; # Autoflush stdout

# Helper procedure: waits for next tick, returns tick hashref or undef when done
sub harness_wait {
    my ($id) = @_;
    print "# \@harness.clock:wait id=$id\n";
    my $line = <STDIN>;
    return undef unless defined $line;

    my ($tag, $clock_id, $cycle, $skipped, $lag_ms, $mono_ts, $status) = split(' ', $line);
    return undef if $status eq 'done';

    return { cycle => $cycle, lag_ms => $lag_ms, mono_ts => $mono_ts, status => $status };
}

# 1. Initialize 5 cycles at 60 FPS (1s / 60)
print "# \@harness.clock:init id=perl interval=\"1s / 60\" cycles=5\n";

# 2. Idiomatic loop using the subroutine
while (my $tick = harness_wait('perl')) {
    print "Perl tick: cycle=$tick->{cycle} (lag: $tick->{lag_ms}ms)\n";
}
