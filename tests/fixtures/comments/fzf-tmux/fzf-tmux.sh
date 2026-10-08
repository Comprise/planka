# источник: fzf 0.74.4, bin/fzf-tmux, строки 141–196
if [[ -z $TMUX ]]; then
  "$fzf" "${args[@]}"
  exit $?
fi

# * --height option is not allowed
# * CTRL-Z is also disabled
# * fzf-tmux script is not compatible with --tmux option in fzf 0.53.0 or later
args=("${args[@]}" "--no-height" "--bind=ctrl-z:ignore" "--no-tmux")

# Handle zoomed tmux pane without popup options by moving it to a temp window
if [[ ! $opt =~ "-E" ]] && tmux list-panes -F '#F' | grep -q Z; then
  zoomed_without_popup=1
  original_window=$(tmux display-message -p "#{window_id}")
  tmp_window=$(tmux new-window -d -P -F "#{window_id}" "bash -c 'while :; do for c in \\| / - '\\;' do sleep 0.2; printf \"\\r\$c fzf-tmux is running\\r\"; done; done'")
  tmux swap-pane -t $tmp_window \; select-window -t $tmp_window
fi

set -e

# Clean up named pipes on exit
tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/fzf-tmux-XXXXXX")
argsf="$tmpdir/args"
fifo1="$tmpdir/fifo1"
fifo2="$tmpdir/fifo2"
fifo3="$tmpdir/fifo3"
if tmux_win_opts=$(tmux show-options -p remain-on-exit \; show-options -p synchronize-panes 2> /dev/null); then
  tmux_win_opts=($(sed '/ off/d; s/synchronize-panes/set-option -p synchronize-panes/; s/remain-on-exit/set-option -p remain-on-exit/; s/$/ \\;/' <<< "$tmux_win_opts"))
  tmux_off_opts='; set-option -p synchronize-panes off ; set-option -p remain-on-exit off'
else
  tmux_win_opts=($(tmux show-window-options remain-on-exit \; show-window-options synchronize-panes | sed '/ off/d; s/^/set-window-option /; s/$/ \\;/'))
  tmux_off_opts='; set-window-option synchronize-panes off ; set-window-option remain-on-exit off'
fi
cleanup() {
  \rm -rf "$tmpdir"
  # Restore tmux window options
  if [[ ${#tmux_win_opts[@]} -gt 1 ]]; then
    eval "tmux ${tmux_win_opts[*]}"
  fi

  # Remove temp window if we were zoomed without popup options
  if [[ -n $zoomed_without_popup ]]; then
    tmux display-message -p "#{window_id}" > /dev/null
    tmux swap-pane -t $original_window \; \
      select-window -t $original_window \; \
      kill-window -t $tmp_window \; \
      resize-pane -Z
  fi

  if [[ $# -gt 0 ]]; then
    trap - EXIT
    exit 130
  fi
}
trap 'cleanup 1' SIGUSR1
trap 'cleanup' EXIT
