#!/usr/bin/env python3
# Simple chess engine for conversation.
# Handles move validation for white (user) and black (random move).
# State persisted in chess_state.json in the current directory.
import json, sys, os, random

STATE_FILE = 'chess_state.json'

# Unicode symbols for pieces.
UNICODE_MAP = {
    'K': '♔', 'Q': '♕', 'R': '♖', 'B': '♗', 'N': '♘', 'P': '♙',
    'k': '♚', 'q': '♛', 'r': '♜', 'b': '♝', 'n': '♞', 'p': '♟',
    '.': '·'
}

def init_state():
    board = [list('rnbqkbnr'),
             list('pppppppp'),
             list('........'),
             list('........'),
             list('........'),
             list('........'),
             list('PPPPPPPP'),
             list('RNBQKBNR')]
    state = {'board': board, 'turn': 'w'}
    save_state(state)
    return state

def load_state():
    if not os.path.exists(STATE_FILE):
        return init_state()
    with open(STATE_FILE) as f:
        data = json.load(f)
    # JSON stores board as list of strings; convert to list of char lists.
    data['board'] = [list(row) for row in data['board']]
    return data

def save_state(state):
    to_save = {'board': [''.join(row) for row in state['board']], 'turn': state['turn']}
    with open(STATE_FILE, 'w') as f:
        json.dump(to_save, f)

def print_board(board):
    lines = []
    header = '  a b c d e f g h'
    lines.append(header)
    for r in range(8):
        rank = 8 - r
        row_symbols = [UNICODE_MAP.get(board[r][c], board[r][c]) for c in range(8)]
        lines.append(f"{rank} " + ' '.join(row_symbols))
    return '\n'.join(lines)

def sq_to_coords(sq):
    if len(sq) != 2:
        raise ValueError('Invalid square')
    file, rank = sq[0], sq[1]
    col = ord(file) - ord('a')
    row = 8 - int(rank)
    if not (0 <= col < 8 and 0 <= row < 8):
        raise ValueError('Square out of range')
    return row, col

def coords_to_sq(row, col):
    return chr(ord('a') + col) + str(8 - row)

def parse_move(move):
    if len(move) != 4:
        return None
    try:
        src = sq_to_coords(move[:2])
        dst = sq_to_coords(move[2:])
        return src + dst
    except Exception:
        return None

def side_of(piece):
    if piece == '.':
        return None
    return 'w' if piece.isupper() else 'b'

def inside(r, c):
    return 0 <= r < 8 and 0 <= c < 8

def generate_moves(board, side):
    moves = []
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if p == '.' or side_of(p) != side:
                continue
            pt = p.upper()
            if pt == 'P':
                moves.extend(pawn_moves(board, r, c, side))
            elif pt == 'N':
                moves.extend(knight_moves(board, r, c, side))
            elif pt == 'B':
                moves.extend(bishop_moves(board, r, c, side))
            elif pt == 'R':
                moves.extend(rook_moves(board, r, c, side))
            elif pt == 'Q':
                moves.extend(queen_moves(board, r, c, side))
            elif pt == 'K':
                moves.extend(king_moves(board, r, c, side))
    return moves

def pawn_moves(board, r, c, side):
    moves = []
    direction = -1 if side == 'w' else 1
    start_row = 6 if side == 'w' else 1
    # one step forward
    nr = r + direction
    if inside(nr, c) and board[nr][c] == '.':
        moves.append((r, c, nr, c))
        # two steps from start
        if r == start_row:
            nr2 = r + 2*direction
            if inside(nr2, c) and board[nr2][c] == '.':
                moves.append((r, c, nr2, c))
    # captures
    for dc in (-1, 1):
        nc = c + dc
        if inside(nr, nc) and board[nr][nc] != '.' and side_of(board[nr][nc]) != side:
            moves.append((r, c, nr, nc))
    return moves

def knight_moves(board, r, c, side):
    moves = []
    deltas = [(2,1),(1,2),(-1,2),(-2,1),(-2,-1),(-1,-2),(1,-2),(2,-1)]
    for dr, dc in deltas:
        nr, nc = r+dr, c+dc
        if not inside(nr, nc): continue
        target = board[nr][nc]
        if target == '.' or side_of(target) != side:
            moves.append((r, c, nr, nc))
    return moves

def sliding_moves(board, r, c, side, dirs):
    moves = []
    for dr, dc in dirs:
        nr, nc = r+dr, c+dc
        while inside(nr, nc):
            target = board[nr][nc]
            if target == '.':
                moves.append((r, c, nr, nc))
            else:
                if side_of(target) != side:
                    moves.append((r, c, nr, nc))
                break
            nr += dr
            nc += dc
    return moves

def bishop_moves(board, r, c, side):
    return sliding_moves(board, r, c, side, [(1,1),(1,-1),(-1,1),(-1,-1)])

def rook_moves(board, r, c, side):
    return sliding_moves(board, r, c, side, [(1,0),(-1,0),(0,1),(0,-1)])

def queen_moves(board, r, c, side):
    return sliding_moves(board, r, c, side, [(1,1),(1,-1),(-1,1),(-1,-1),(1,0),(-1,0),(0,1),(0,-1)])

def king_moves(board, r, c, side):
    moves = []
    for dr in (-1,0,1):
        for dc in (-1,0,1):
            if dr == 0 and dc == 0:
                continue
            nr, nc = r+dr, c+dc
            if not inside(nr, nc): continue
            target = board[nr][nc]
            if target == '.' or side_of(target) != side:
                moves.append((r, c, nr, nc))
    return moves

def apply_move(board, src_r, src_c, dst_r, dst_c):
    piece = board[src_r][src_c]
    # Promotion: pawn reaching last rank becomes queen.
    if piece.upper() == 'P' and (dst_r == 0 or dst_r == 7):
        piece = 'Q' if piece.isupper() else 'q'
    board[dst_r][dst_c] = piece
    board[src_r][src_c] = '.'
    return board

def find_king(board, side):
    king = 'K' if side == 'w' else 'k'
    for r in range(8):
        for c in range(8):
            if board[r][c] == king:
                return (r, c)
    return None

def main():
    state = load_state()
    board = state['board']
    turn = state['turn']
    # If no argument: just display board.
    if len(sys.argv) == 1:
        print(print_board(board))
        return
    # User move supplied.
    user_move = sys.argv[1].strip()
    if turn != 'w':
        print('It is not white\'s turn.', file=sys.stderr)
        sys.exit(1)
    parsed = parse_move(user_move)
    if not parsed:
        print(f'Invalid move format: {user_move}. Use e2e4.', file=sys.stderr)
        sys.exit(1)
    src_r, src_c, dst_r, dst_c = parsed
    piece = board[src_r][src_c]
    if piece == '.' or side_of(piece) != 'w':
        print(f'No white piece on {user_move[:2]}.', file=sys.stderr)
        sys.exit(1)
    # verify legality
    legal = generate_moves(board, 'w')
    if (src_r, src_c, dst_r, dst_c) not in legal:
        print(f'Illegal move: {user_move}', file=sys.stderr)
        sys.exit(1)
    # apply white move
    board = apply_move(board, src_r, src_c, dst_r, dst_c)
    # check for black king capture
    if find_king(board, 'b') is None:
        state['board'] = board
        state['turn'] = 'w'
        save_state(state)
        print('White wins! Black king captured.')
        print(print_board(board))
        return
    # Black's move (random)
    black_moves = generate_moves(board, 'b')
    if not black_moves:
        state['board'] = board
        state['turn'] = 'w'
        save_state(state)
        print('Black has no legal moves. Game over.')
        print(print_board(board))
        return
    bm = random.choice(black_moves)
    br, bc, brd, bcd = bm
    black_move = coords_to_sq(br, bc) + coords_to_sq(brd, bcd)
    board = apply_move(board, br, bc, brd, bcd)
    # check for white king capture
    if find_king(board, 'w') is None:
        state['board'] = board
        state['turn'] = 'w'
        save_state(state)
        print(f'Black plays {black_move}')
        print('Black wins! White king captured.')
        print(print_board(board))
        return
    # update state
    state['board'] = board
    state['turn'] = 'w'
    save_state(state)
    print(f'Black plays {black_move}')
    print(print_board(board))

if __name__ == '__main__':
    main()
