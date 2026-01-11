import socket
import struct
import threading
import time
import random

BROADCAST_PORT = 13122
SERVER_PORT = 0  # let OS choose
OFFER_COOKIE = 0xabcddcba
OFFER_TYPE = 0x2

REQUEST_COOKIE = 0xabcddcba
REQUEST_TYPE = 0x3

PAYLOAD_COOKIE = 0xabcddcba
PAYLOAD_TYPE = 0x4

RESULT_NOT_OVER = 0
RESULT_WIN = 1
RESULT_LOSS = 2
RESULT_TIE = 3


def build_deck():
    # ranks 1..13, suits 0..3
    deck = [(r, s) for r in range(1, 14) for s in range(4)]
    random.shuffle(deck)
    return deck


def card_value(rank: int) -> int:
    if rank == 1:
        return 11
    if 2 <= rank <= 10:
        return rank
    return 10


def adjust_for_aces(total: int, ace_count: int) -> int:
    while total > 21 and ace_count > 0:
        total -= 10
        ace_count -= 1
    return total


def recv_exact(conn: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Client disconnected.")
        data += chunk
    return data


def read_request(conn: socket.socket):
    # request + \n
    fmt = "!IbB32s"
    size = struct.calcsize(fmt)
    data = recv_exact(conn, size)
    # consume trailing newline (optional robustness)
    conn.recv(1)

    cookie, msg_type, rounds, name_bytes = struct.unpack(fmt, data)
    if cookie != REQUEST_COOKIE or msg_type != REQUEST_TYPE:
        raise ValueError("Bad request header")
    name = name_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return rounds, name


def read_client_decision(conn: socket.socket, timeout_sec: float = 10.0) -> bytes:
    """Return 5-byte decision payload: b'Hittt' or b'Stand'"""
    fmt = "!Ib5s"
    size = struct.calcsize(fmt)

    prev_timeout = conn.gettimeout()
    conn.settimeout(timeout_sec)
    try:
        data = recv_exact(conn, size)
    finally:
        conn.settimeout(prev_timeout)

    cookie, msg_type, decision = struct.unpack(fmt, data)
    if cookie != PAYLOAD_COOKIE or msg_type != PAYLOAD_TYPE:
        raise ValueError("Bad payload header")
    if decision not in (b"Hittt", b"Stand"):
        raise ValueError("Bad decision")
    return decision


def send_payload(conn: socket.socket, result: int, rank: int, suit: int):
    fmt = "!IbBHB"
    conn.sendall(struct.pack(fmt, PAYLOAD_COOKIE, PAYLOAD_TYPE, result, rank, suit))


def broadcast_offers(server_tcp_port: int, server_name: str, stop_event: threading.Event):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    fmt = "!IbH32s"
    name_bytes = server_name.encode("utf-8")[:32].ljust(32, b"\x00")
    pkt = struct.pack(fmt, OFFER_COOKIE, OFFER_TYPE, server_tcp_port, name_bytes)

    while not stop_event.is_set():
        udp.sendto(pkt, ("<broadcast>", BROADCAST_PORT))
        time.sleep(1.0)

    udp.close()


def play_round(conn: socket.socket):
    deck = build_deck()

    def draw():
        return deck.pop()

    # player & dealer hands
    player = [draw(), draw()]
    dealer = [draw(), draw()]  # dealer[1] is hidden initially

    # totals tracking with aces
    def hand_total(hand):
        total = 0
        aces = 0
        for r, s in hand:
            total += card_value(r)
            if r == 1:
                aces += 1
        return adjust_for_aces(total, aces)

    # send 2 player cards, then dealer up-card
    send_payload(conn, RESULT_NOT_OVER, player[0][0], player[0][1])
    send_payload(conn, RESULT_NOT_OVER, player[1][0], player[1][1])
    send_payload(conn, RESULT_NOT_OVER, dealer[0][0], dealer[0][1])

    # player turn
    while True:
        pt = hand_total(player)
        if pt > 21:
            # bust should be caught earlier usually, but safe anyway
            return RESULT_LOSS

        decision = read_client_decision(conn, timeout_sec=10.0)
        if decision == b"Stand":
            break

        # Hit
        card = draw()
        player.append(card)
        pt = hand_total(player)
        if pt > 21:
            send_payload(conn, RESULT_LOSS, card[0], card[1])
            return RESULT_LOSS
        else:
            send_payload(conn, RESULT_NOT_OVER, card[0], card[1])

    # dealer turn: reveal hidden
    send_payload(conn, RESULT_NOT_OVER, dealer[1][0], dealer[1][1])

    # hit until >= 17
    while hand_total(dealer) < 17:
        card = draw()
        dealer.append(card)
        send_payload(conn, RESULT_NOT_OVER, card[0], card[1])

    # decide winner
    pt = hand_total(player)
    dt = hand_total(dealer)

    if dt > 21:
        # dealer bust: send a final payload (no extra card here; we need to end round cleanly)
        # We'll end by sending one "dummy" card? Better: end on last dealer card already sent.
        # So: just return WIN; client will see final on last dealer card? Not yet.
        # To keep protocol identical to your approach, we send a final payload with last dealer card and result.
        last = dealer[-1]
        send_payload(conn, RESULT_WIN, last[0], last[1])
        return RESULT_WIN

    if pt > dt:
        last = dealer[-1]
        send_payload(conn, RESULT_WIN, last[0], last[1])
        return RESULT_WIN
    if pt < dt:
        last = dealer[-1]
        send_payload(conn, RESULT_LOSS, last[0], last[1])
        return RESULT_LOSS

    last = dealer[-1]
    send_payload(conn, RESULT_TIE, last[0], last[1])
    return RESULT_TIE


def handle_client(conn: socket.socket, addr):
    try:
        rounds, name = read_request(conn)
        print(f"Player connected: {name} from {addr}, rounds={rounds}")

        for _ in range(rounds):
            play_round(conn)

    except Exception as e:
        print(f"Client error {addr}: {e}")
    finally:
        conn.close()


def main():
    server_name = "BlackjackServer"

    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.bind(("", SERVER_PORT))
    tcp.listen()
    server_tcp_port = tcp.getsockname()[1]
    print(f"Server listening on TCP port {server_tcp_port}")

    stop_event = threading.Event()
    t = threading.Thread(target=broadcast_offers, args=(server_tcp_port, server_name, stop_event), daemon=True)
    t.start()

    try:
        while True:
            conn, addr = tcp.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    finally:
        stop_event.set()
        tcp.close()


if __name__ == "__main__":
    main()
