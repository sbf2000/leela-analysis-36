import hashlib
import math
import os
import pickle
import sys
import time
import traceback
import config
import re
import datetime
from sgftools import regex
import sgftools.utils as utils
from sgftools import gotools, annotations, progressbar, sgflib
from sgftools.leela import Leela


def calculate_tasks_left(sgf_file, request_analyze, request_variations):
    """
    Calculate tasks left
    """
    sgf_cursor = sgf_file.cursor()
    move_num = 0
    analyze_tasks = 0
    variations_tasks = 0

    # Look through every node
    while not sgf_cursor.atEnd:
        sgf_cursor.next()
        analysis_mode = None

        # Conditions
        is_default_analysis = args.analyze_start <= move_num <= args.analyze_end
        is_analyze_request = move_num in request_analyze
        is_prev_analyze_request = (move_num - 1) in request_analyze
        is_vars_request = move_num in request_variations
        is_prev_vars_request = (move_num - 1) in request_variations

        if is_vars_request:
            analyze_tasks += 1
            variations_tasks += 1
        elif any([is_default_analysis, is_analyze_request, is_prev_analyze_request, is_prev_vars_request]):
            analyze_tasks += 1

        move_num += 1

    return analyze_tasks, variations_tasks


def add_moves_to_leela(cursor, leela):
    this_move = None

    # Get moves nodes
    node_black_move = cursor.node.get('B')
    node_white_move = cursor.node.get('W')
    node_black_stones = cursor.node.get('AB')
    node_white_stones = cursor.node.get('AW')

    # Store commands to add black or white moves
    if node_black_move:
        this_move = node_black_move.data[0]
        leela.add_move('black', this_move)

    if node_white_move:
        this_move = node_white_move.data[0]
        leela.add_move('white', this_move)

    # Store commands to add black or white stones, often used for setting up handicap and such
    if node_black_stones:
        for move in node_black_stones.data:
            leela.add_move('black', move)

    if node_white_stones:
        for move in node_white_stones.data:
            leela.add_move('white', move)

    return this_move


def do_analyze(leela, base_dir, seconds_per_search):
    ckpt_hash = 'analyze_' + leela.history_hash() + "_" + str(seconds_per_search) + "sec"
    ckpt_fn = os.path.join(base_dir, ckpt_hash)

    if args.verbosity > 2:
        print("Looking for checkpoint file: %s" % ckpt_fn, file=sys.stderr)

    if os.path.exists(ckpt_fn):
        if args.verbosity > 2:
            print("Loading checkpoint file: %s" % ckpt_fn, file=sys.stderr)

        with open(ckpt_fn, 'rb') as ckpt_file:
            stats, move_list = pickle.load(ckpt_file)
            ckpt_file.close()
    else:
        leela.reset()
        leela.go_to_position()
        stats, move_list = leela.analyze(seconds_per_search)
        with open(ckpt_fn, 'wb') as ckpt_file:
            pickle.dump((stats, move_list), ckpt_file)
            ckpt_file.close()

    return stats, move_list


def next_move_pos(cursor):
    """
    Go to next node and look what move was played
    :return: SGF-like move string
    """
    next_move = None

    if not cursor.atEnd:
        cursor.next()
        node_white_move = cursor.node.get('W')
        node_black_move = cursor.node.get('B')

        if node_white_move:
            next_move = node_white_move.data[0]
        elif node_black_move:
            next_move = node_black_move.data[0]

        cursor.previous()

    return next_move


if __name__ == '__main__':

    time_start = datetime.datetime.now()

    args = config.parser.parse_args()

    if args.verbosity > 0:
        print("Leela analysis started at %s" % time_start, file=sys.stderr)
        print("Game moves analysis: %d seconds per move" % args.analyze_time, file=sys.stderr)
        print("Variations analysis: %d seconds per move" % args.variations_time, file=sys.stderr)

    sgf_fn = args.SGF_FILE

    if not args.save_to_file:
        # FIXME: possible bug with folders which contain "." in their names
        args.save_to_file = args.SGF_FILE.split('.')[0] + '_analyzed.sgf'

    if not os.path.exists(sgf_fn):
        config.parser.error("No such file: %s" % sgf_fn)
    sgf = gotools.import_sgf(sgf_fn)

    RESTART_COUNT = args.restarts

    # Create checkpoints directory f not exists
    if not os.path.exists(args.ckpt_dir):
        os.mkdir(args.ckpt_dir)

    # Create base hash and directory for analysis
    base_hash = hashlib.md5(os.path.abspath(sgf_fn).encode()).hexdigest()
    base_dir = os.path.join(args.ckpt_dir, base_hash)

    # Create base if not exists
    if not os.path.exists(base_dir):
        os.mkdir(base_dir)

    if args.verbosity > 1:
        print("Checkpoint dir: %s" % base_dir, file=sys.stderr)

    # Set up SGF cursor
    cursor = sgf.cursor()

    # Get initial nodes
    node_boardsize = cursor.node.get('SZ')
    node_handicap = cursor.node.get('HA')
    node_rules = cursor.node.get('RU')
    node_komi = cursor.node.get('KM')

    # Set board size
    board_size = int(node_boardsize.data[0]) if node_boardsize else 19

    if board_size != 19:
        print("Warning: board size is not 19 so Leela could be much weaker and less accurate", file=sys.stderr)

        if args.analyze_threshold == config.defaults['analyze_threshold'] \
                or args.variations_threshold == config.defaults['variations_threshold']:
            print("Warning: Consider also setting --analyze-thresh and --var-thresh higher", file=sys.stderr)

    # Set handicap stones count
    if node_handicap and int(node_handicap.data[0]) > 1:
        handicap_stone_count = int(node_handicap.data[0])
    else:
        handicap_stone_count = 0

    is_handicap_game = bool(handicap_stone_count)

    # Set rules
    is_japanese_rules = node_rules and node_rules.data[0].lower() in ['jp', 'japanese', 'japan']

    # Set komi
    if node_komi:
        komi = float(node_komi.data[0])

        if is_handicap_game and is_japanese_rules:
            old_komi = komi
            komi = old_komi + handicap_stone_count
            print("Adjusting komi from %.1f to %.1f in converting Japanese rules with %d handicap to Chinese rules" % (
                old_komi, komi, handicap_stone_count), file=sys.stderr)
    else:
        if is_handicap_game:
            komi = 0.5 if is_japanese_rules else 0.5 + handicap_stone_count
        else:
            komi = 6.5 if is_japanese_rules else 7.5
        print("Warning: Komi not specified, assuming %.1f" % komi, file=sys.stderr)
    print(komi)

    # COLLECT MOVES REQUESTED FOR ANALYSIS AND VARIATIONS
    comment_requests_analyze = {}  # will contain moves requested for analysis
    comment_requests_variations = {}  # will contain moves requested for varioations
    move_num = -1

    while not cursor.atEnd:

        # Go to next node and increment move_num
        cursor.next()
        move_num += 1

        node_comment = cursor.node.get('C')

        # Store moves, requested for analysis and variations
        if node_comment:
            match = re.match(regex.comment_regex, node_comment.data[0])

            if 'analyze' in match.group('node_comment'):
                comment_requests_analyze[move_num] = True

            if 'variations' in match.group('node_comment'):
                comment_requests_analyze[move_num] = True
                comment_requests_variations[move_num] = True

            # Wipe comments is needed
            if args.wipe_comments:
                node_comment.data[0] = ""

    # END OF SGF-FILE REACHED

    # Count initial analyze and variations tasks
    (analyze_tasks_initial, variations_tasks_initial) = calculate_tasks_left(sgf, comment_requests_analyze,
                                                                             comment_requests_variations)
    print(analyze_tasks_initial, variations_tasks_initial)
    variations_task_probability = 1.0 / (1.0 + args.variations_threshold * 100.0)
    analyze_tasks_initial_done = 0
    variations_tasks = variations_tasks_initial
    variations_tasks_done = 0


    def approx_tasks_done():
        """
        Calculate approximate tasks done
        """
        return (
            analyze_tasks_initial_done +
            (variations_tasks_done * args.nodes_per_variation)
        )

    def approx_tasks_max():
        """
        Calculate approximate tasks max
        """
        return (
            (analyze_tasks_initial - analyze_tasks_initial_done) *
            (1 + variations_task_probability * args.nodes_per_variation) +
            analyze_tasks_initial_done +
            (variations_tasks * args.nodes_per_variation)
        )

    transform_winrate = utils.winrate_transformer(config.defaults['stdev'], args.verbosity)

    analyze_threshold = transform_winrate(0.5 + 0.5 * args.analyze_threshold) - transform_winrate(
        0.5 - 0.5 * args.analyze_threshold)
    variations_threshold = transform_winrate(0.5 + 0.5 * args.variations_threshold) - transform_winrate(
        0.5 - 0.5 * args.variations_threshold)

    print("Executing approx %d analysis steps" % approx_tasks_max(), file=sys.stderr)

    progress_bar = progressbar.ProgressBar(max_value=approx_tasks_max())
    progress_bar.start()


    def refresh_progress_bar():
        """
        Refresh progress bar
        """
        progress_bar.update(approx_tasks_done(), approx_tasks_max())


    # Create Leela
    leela = Leela(board_size=board_size,
                  executable=args.executable,
                  is_handicap_game=is_handicap_game,
                  komi=komi,
                  seconds_per_search=args.analyze_time,
                  verbosity=args.verbosity)

    collected_winrates = {}
    collected_best_moves = {}
    collected_best_move_winrates = {}
    needs_variations = {}

    try:
        cursor = sgf.cursor()
        move_num = -1
        prev_stats = {}
        prev_move_list = []
        has_previous = False

        # Start Leela process
        leela.start()
        add_moves_to_leela(cursor, leela)

        # ANALYZE MAIN LINE AND COLLECT WINRATES
        while not cursor.atEnd:
            cursor.next()
            move_num += 1

            # Add move to history and get whose turn is now
            this_move = add_moves_to_leela(cursor, leela)
            current_player = leela.whose_turn()
            prev_player = "white" if current_player == "black" else "black"

            # Conditions to start analysis
            is_default_analysis = args.analyze_start <= move_num <= args.analyze_end
            is_analyze_request = move_num in comment_requests_analyze
            is_prev_analyze_request = (move_num - 1) in comment_requests_analyze
            is_vars_request = move_num in comment_requests_variations
            is_prev_vars_request = (move_num - 1) in comment_requests_variations

            # START MAIN LINE ANALYSIS
            if any([is_default_analysis, is_analyze_request, is_prev_analyze_request,
                    is_vars_request, is_prev_vars_request]):
                stats, move_list = do_analyze(leela, base_dir, args.analyze_time)

                # Store winrate of black player after played move
                if 'winrate' in stats and stats['visits'] > 100:
                    collected_winrates[move_num] = (current_player, stats['winrate'])

                # Store best move winrate
                if len(move_list) > 0 and 'winrate' in move_list[0]:
                    collected_best_moves[move_num] = move_list[0]['pos']
                    collected_best_move_winrates[move_num] = move_list[0]['winrate']

                delta = 0.0
                transdelta = 0.0

                # If winrate of current and previous moves exist, calculate delta
                if 'winrate' in stats and (move_num - 1) in collected_best_moves:
                    # Calculate delta and transdelta if played move is not Leela's "best move"
                    if this_move != collected_best_moves[move_num - 1]:
                        delta = stats['winrate'] - collected_best_move_winrates[move_num - 1]
                        delta = min(0.0, (-delta if leela.whose_turn() == "black" else delta))  # adjust delta <= 0
                        transdelta = transform_winrate(stats['winrate']) - \
                                     transform_winrate(collected_best_move_winrates[move_num - 1])
                        transdelta = min(0.0, (-transdelta if leela.whose_turn() == "black" else transdelta))

                    if transdelta <= -analyze_threshold:
                        (delta_comment, delta_lb_values) = annotations.format_delta_info(delta, transdelta, stats,
                                                                                         this_move, board_size)
                        annotations.annotate_sgf(cursor, delta_comment, delta_lb_values)

                is_reached_analyze_thresh = transdelta <= -analyze_threshold
                is_reached_vars_thresh = transdelta <= -variations_threshold
                is_skipped_for_vars = (args.skip_white and prev_player == "white") or \
                                      (args.skip_black and prev_player == "black")

                # If previous move was analyzed and current move is not skipped,
                # store moves that should be analyzed with variations
                if has_previous and not is_skipped_for_vars and (is_reached_vars_thresh or is_prev_vars_request):
                    needs_variations[move_num - 1] = (prev_stats, prev_move_list)

                    # Increment counter if previous move is not comment requested
                    if not is_prev_vars_request:
                        variations_tasks += 1

                # Look where the next move was played
                next_game_move = next_move_pos(cursor)

                # Add winrate comment
                winrate_comment = annotations.format_winrate(stats, move_list, board_size, next_game_move)
                annotations.annotate_sgf(cursor, winrate_comment)

                # Add comment with analysis to previous move
                if has_previous and (is_prev_analyze_request or is_prev_vars_request or is_reached_analyze_thresh) and \
                        not is_skipped_for_vars:
                    (analysis_comment, lb_values, tr_values) = annotations.format_analysis(
                        prev_stats, prev_move_list, this_move)
                    cursor.previous()
                    annotations.annotate_sgf(cursor, analysis_comment, lb_values, tr_values)
                    cursor.next()

                # Save move information for next move analysis
                prev_stats = stats
                prev_move_list = move_list
                has_prev = True
                analyze_tasks_initial_done += 1  # count completed task

                # Update sgf-file and refresh progressbar
                utils.write_to_file(args.save_to_file, 'w', sgf)
                refresh_progress_bar()

            # SKIP MAIN LINE ANALYSIS
            else:
                prev_stats = {}
                prev_move_list = []
                has_prev = False

            # END MAIN LINE ANALYSIS

        # Stop leela process and clear history
        leela.stop()
        leela.clear_history()

        # Save winrate graph to file
        if args.win_graph:
            utils.graph_winrates(collected_winrates, args.SGF_FILE)

        utils.write_to_file(args.save_to_file, 'w', sgf)

    except:
        traceback.print_exc()
        print("Failure, reporting partial results...", file=sys.stderr)
    finally:
        leela.stop()
    """
    END
    """

    utils.write_to_file(args.save_to_file, 'w', sgf)
    time_stop = datetime.datetime.now()

    if args.verbosity > 0:
        print("Leela analysis stopped at %s" % time_stop, file=sys.stderr)
        print("Elapsed time: %s" % (time_stop - time_start), file=sys.stderr)
