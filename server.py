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
    """
    Build and shuffle a standard 52-card deck.

    Cards are represented as (rank, suit) tuples,
    where rank is 1-13 and suit is 0-3.

    Returns:
        list[tuple[int, int]]: Shuffled deck of cards.
    """
    # ranks 1..13, suits 0..3
    deck = [(r, s) for r in range(1, 14) for s in range(4)]
    random.shuffle(deck)
    return deck


def card_value(rank: int) -> int:
    """
    Convert a card rank to its Blackjack value.

    Ace is initially counted as 11.
    Face cards (J, Q, K) are counted as 10.

    Args:
        rank (int): Card rank (1-13).

    Returns:
        int: Blackjack value of the card.
    """
    if rank == 1:
        return 11
    if 2 <= rank <= 10:
        return rank
    return 10


def adjust_for_aces(total: int, ace_count: int) -> int:
    """
    Adjust hand total by converting Aces from 11 to 1 if needed.

    Prevents busting when total exceeds 21.

    Args:
        total (int): Current hand total.
        ace_count (int): Number of Aces counted as 11.

    Returns:
        int: Adjusted hand total.
    """
    while total > 21 and ace_count > 0:
        total -= 10
        ace_count -= 1
    return total


def recv_exact(conn: socket.socket, n: int) -> bytes:
    """
    Receive exactly n bytes from a TCP connection.

    TCP may return fewer bytes than requested in a single recv call,
    so this function loops until all bytes are received.

    Args:
        conn (socket.socket): Connected TCP socket.
        n (int): Number of bytes to receive.

    Returns:
        bytes: Exactly n bytes.

    Raises:
        ConnectionError: If the client disconnects prematurely.
    """
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Client disconnected.")
        data += chunk
    return data


def read_request(conn: socket.socket):
    """
    Read and parse the initial join request from the client.

    The request contains the protocol cookie, message type,
    number of rounds, and client name.

    Args:
        conn (socket.socket): Connected TCP socket.

    Returns:
        tuple[int, str]: (rounds, player_name)

    Raises:
        ValueError: If the request header is invalid.
    """
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
    """
    Read a Hit or Stand decision from the client.

    The decision is received as a fixed-size payload
    and validated against the protocol.

    Args:
        conn (socket.socket): Connected TCP socket.
        timeout_sec (float): Timeout in seconds.

    Returns:
        bytes: Client decision (b'Hittt' or b'Stand').

    Raises:
        ValueError: If the payload is invalid.
    """
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
    """
    Send a payload message to the client.

    Payloads include the current game result
    and card information.

    Args:
        conn (socket.socket): Connected TCP socket.
        result (int): Game result code.
        rank (int): Card rank.
        suit (int): Card suit.
    """
    fmt = "!IbBHB"
    conn.sendall(struct.pack(fmt, PAYLOAD_COOKIE, PAYLOAD_TYPE, result, rank, suit))


def broadcast_offers(server_tcp_port: int, server_name: str, stop_event: threading.Event):
    """
    Broadcast server offers periodically using UDP.

    Continues broadcasting until stop_event is set.

    Args:
        server_tcp_port (int): TCP port of the server.
        server_name (str): Name of the server.
        stop_event (threading.Event): Event to stop broadcasting.
    """
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
    """
    Play a single round of Blackjack with a connected client.

    Handles dealing cards, player decisions,
    dealer logic, and determining the final result.

    Args:
        conn (socket.socket): Connected TCP socket.

    Returns:
        int: Final result code of the round.
    """
    deck = build_deck()

    def draw():
        # Draw the top card from the deck
        return deck.pop()

    # player & dealer hands
    player = [draw(), draw()]
    dealer = [draw(), draw()]  # dealer[1] is hidden initially

    # calculate hand total with ace adjustment
    def hand_total(hand):
        total = 0
        aces = 0
        for r, s in hand:
            total += card_value(r)
            if r == 1:
                aces += 1
        return adjust_for_aces(total, aces)

    # send initial cards: 2 player cards and 1 dealer up-card
    send_payload(conn, RESULT_NOT_OVER, player[0][0], player[0][1])
    send_payload(conn, RESULT_NOT_OVER, player[1][0], player[1][1])
    send_payload(conn, RESULT_NOT_OVER, dealer[0][0], dealer[0][1])

    # player turn
    while True:
        pt = hand_total(player)
        if pt > 21:
            return RESULT_LOSS

        decision = read_client_decision(conn, timeout_sec=10.0)
        if decision == b"Stand":
            break

        card = draw()
        player.append(card)
        pt = hand_total(player)
        if pt > 21:
            send_payload(conn, RESULT_LOSS, card[0], card[1])
            return RESULT_LOSS
        else:
            send_payload(conn, RESULT_NOT_OVER, card[0], card[1])

    # dealer turn: reveal hidden card
    send_payload(conn, RESULT_NOT_OVER, dealer[1][0], dealer[1][1])

    # dealer hits until reaching at least 17
    while hand_total(dealer) < 17:
        card = draw()
        dealer.append(card)
        send_payload(conn, RESULT_NOT_OVER, card[0], card[1])

    pt = hand_total(player)
    dt = hand_total(dealer)

    if dt > 21:
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
    """
    Handle a single client connection.

    Reads the join request and runs the requested
    number of game rounds.

    Args:
        conn (socket.socket): Client TCP socket.
        addr (tuple): Client address.
    """
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
    """
    Main entry point for the Blackjack server.

    Sets up the TCP server, broadcasts offers via UDP,
    and spawns a new thread for each connected client.
    """
    server_name = "BlackjackServer"

    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.bind(("", SERVER_PORT))
    tcp.listen()
    server_tcp_port = tcp.getsockname()[1]
    print(f"Server listening on TCP port {server_tcp_port}")

    stop_event = threading.Event()
    t = threading.Thread(
        target=broadcast_offers,
        args=(server_tcp_port, server_name, stop_event),
        daemon=True
    )
    t.start()

    try:
        while True:
            conn, addr = tcp.accept()
            threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True
            ).start()
    finally:
        stop_event.set()
        tcp.close()


if __name__ == "__main__":
    main()
