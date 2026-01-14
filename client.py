import socket
import struct
import random

UDP_PORT = 13122

# Protocol constants
OFFER_COOKIE = 0xabcddcba
OFFER_TYPE = 0x2

REQUEST_COOKIE = 0xabcddcba
REQUEST_TYPE = 0x3

PAYLOAD_COOKIE = 0xabcddcba
PAYLOAD_TYPE = 0x4


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """
    Receive exactly n bytes from a TCP socket.

    TCP does not guarantee that recv() returns all requested bytes at once.
    This function keeps receiving data until exactly n bytes are collected
    or the socket is closed.

    Args:
        sock (socket.socket): Connected TCP socket.
        n (int): Number of bytes to receive.

    Returns:
        bytes: Exactly n bytes read from the socket.

    Raises:
        ConnectionError: If the socket closes before n bytes are received.
    """
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Socket closed while receiving data.")
        data += chunk
    return data


def parse_offer(pkt: bytes):
    """
    Parse a UDP offer packet according to the protocol.

    The function validates the magic cookie and message type,
    and extracts the server TCP port and server name.

    Args:
        pkt (bytes): Raw UDP packet.

    Returns:
        tuple[int, str] | None: (server_port, server_name) if valid,
        otherwise None.
    """
    fmt = "!IbH32s"
    if len(pkt) < struct.calcsize(fmt):
        return None
    cookie, msg_type, port, name_bytes = struct.unpack(fmt, pkt[:struct.calcsize(fmt)])
    if cookie != OFFER_COOKIE or msg_type != OFFER_TYPE:
        return None
    server_name = name_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return port, server_name


def ask_rounds() -> int:
    """
    Prompt the user to enter the number of rounds.

    Ensures the input is a valid integer between 1 and 255,
    as required by the protocol.

    Returns:
        int: Number of rounds to play.
    """
    while True:
        s = input("Enter number of rounds (1-255): ").strip()
        try:
            r = int(s)
            if 1 <= r <= 255:
                return r
        except ValueError:
            pass
        print("Invalid number. Must be 1-255.")


def ask_choice() -> bytes:
    """
    Prompt the user to choose Hit or Stand.

    The returned value is always exactly 5 bytes,
    matching the protocol specification.

    Returns:
        bytes: b'Hittt' or b'Stand'
    """
    while True:
        c = input("Choose: [H]it or [S]tand: ").strip().lower()
        if c in ("h", "hit"):
            return b"Hittt"
        if c in ("s", "stand"):
            return b"Stand"
        print("Invalid choice. Enter H or S.")


def card_value(rank: int) -> int:
    """
    Convert a card rank to its Blackjack value.

    Ace is initially counted as 11. Face cards
    (J, Q, K) are counted as 10.

    Args:
        rank (int): Card rank (1-13).

    Returns:
        int: Blackjack value of the card.
    """
    # rank: 1..13 (A..K)
    if rank == 1:
        return 11  # initial Ace as 11; we will adjust with ace_count
    if 2 <= rank <= 10:
        return rank
    return 10  # J,Q,K


def card_str(rank: int, suit: int) -> str:
    """
    Convert a card rank and suit to a readable string.

    Used only for display purposes.

    Args:
        rank (int): Card rank.
        suit (int): Card suit.

    Returns:
        str: String representation of the card.
    """
    ranks = {1: "A", 11: "J", 12: "Q", 13: "K"}
    suits = {0: "♠", 1: "♥", 2: "♦", 3: "♣"}
    r = ranks.get(rank, str(rank))
    s = suits.get(suit, "?")
    return f"{r}{s}"


def adjust_for_aces(total: int, ace_count: int) -> int:
    """
    Adjust the hand total by converting Aces from 11 to 1 if needed.

    Prevents busting when the total exceeds 21 and Aces are present.

    Args:
        total (int): Current hand total.
        ace_count (int): Number of Aces counted as 11.

    Returns:
        int: Adjusted hand total.
    """
    # turn some Aces from 11 to 1 as needed
    while total > 21 and ace_count > 0:
        total -= 10
        ace_count -= 1
    return total


def receive_payload(tcp: socket.socket):
    """
    Receive and parse a payload message from the TCP server.

    Validates the protocol cookie and message type,
    and extracts the result and card information.

    Args:
        tcp (socket.socket): Connected TCP socket.

    Returns:
        tuple[int, int, int]: (result, rank, suit)

    Raises:
        ValueError: If the payload is invalid.
        ConnectionError: If the connection closes unexpectedly.
    """
    fmt = "!IbBHB"
    size = struct.calcsize(fmt)
    data = recv_exact(tcp, size)
    cookie, msg_type, result, rank, suit = struct.unpack(fmt, data)

    if cookie != PAYLOAD_COOKIE or msg_type != PAYLOAD_TYPE:
        raise ValueError("Invalid payload header (cookie/type mismatch).")

    # Basic sanity check for card values
    if not (1 <= rank <= 13) or not (0 <= suit <= 3):
        raise ValueError(f"Invalid card from server: rank={rank}, suit={suit}")

    return result, rank, suit


def main():
    """
    Main entry point of the Blackjack client.

    Handles:
    - Server discovery via UDP
    - TCP connection and join request
    - Game loop with state machine
    - User interaction and result handling
    - Graceful shutdown on completion or error
    """
    player_name = input("Enter your name: ").strip()
    if not player_name:
        player_name = f"Player{random.randint(1000, 9999)}"

    name_bytes = player_name.encode("utf-8")[:32].ljust(32, b"\x00")
    rounds = ask_rounds()

    # Listen for UDP offers
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind(("", UDP_PORT))
    udp.settimeout(10.0)

    print(f"Client started, listening for offers on UDP port {UDP_PORT}...")

    server_addr = None
    server_port = None
    server_name = None

    while True:
        try:
            pkt, addr = udp.recvfrom(1024)
        except socket.timeout:
            print("No offers received (timeout). Still listening...")
            continue

        parsed = parse_offer(pkt)
        if not parsed:
            continue

        server_port, server_name = parsed
        server_addr = addr[0]
        print(f"Received offer from {server_name} at {server_addr}, port {server_port}")
        break

    udp.close()

    # Connect to server via TCP
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.settimeout(10.0)
    tcp.connect((server_addr, server_port))
    print("Connected to server. Sending join request...")

    # Send join request
    req_fmt = "!IbB32s"
    tcp.sendall(struct.pack(req_fmt, REQUEST_COOKIE, REQUEST_TYPE, rounds, name_bytes) + b"\n")

    # Per-round game loop
    for round_idx in range(1, rounds + 1):
        print(f"\n=== Round {round_idx}/{rounds} ===")

        # Initialize round state
        player_total = 0
        player_aces = 0
        dealer_total = 0
        dealer_aces = 0

        INIT, PLAYER_TURN, DEALER_TURN, DONE = "INIT", "PLAYER_TURN", "DEALER_TURN", "DONE"
        state = INIT

        init_player_cards_left = 2
        init_dealer_up_left = 1

        try:
            while state != DONE:
                result, rank, suit = receive_payload(tcp)
                card = card_str(rank, suit)

                if state == INIT:
                    if init_player_cards_left > 0:
                        v = card_value(rank)
                        player_total += v
                        if rank == 1:
                            player_aces += 1
                        player_total = adjust_for_aces(player_total, player_aces)

                        print(f"Player card: {card} (total: {player_total})")
                        init_player_cards_left -= 1

                    elif init_dealer_up_left > 0:
                        v = card_value(rank)
                        dealer_total += v
                        if rank == 1:
                            dealer_aces += 1
                        dealer_total = adjust_for_aces(dealer_total, dealer_aces)

                        print(f"Dealer shows: {card}")
                        init_dealer_up_left -= 1

                        state = PLAYER_TURN

                        choice = ask_choice()
                        pay_fmt = "!Ib5s"
                        tcp.sendall(struct.pack(pay_fmt, PAYLOAD_COOKIE, PAYLOAD_TYPE, choice))

                        if choice == b"Stand":
                            state = DEALER_TURN
                    else:
                        raise RuntimeError("INIT state desync.")
                    continue

                if state == PLAYER_TURN:
                    if result != 0:
                        print(f"(Server ended round early) Card: {card}")
                        state = DONE
                        final_result = result
                        break

                    v = card_value(rank)
                    player_total += v
                    if rank == 1:
                        player_aces += 1
                    player_total = adjust_for_aces(player_total, player_aces)

                    print(f"Player hit: {card} (total: {player_total})")

                    choice = ask_choice()
                    pay_fmt = "!Ib5s"
                    tcp.sendall(struct.pack(pay_fmt, PAYLOAD_COOKIE, PAYLOAD_TYPE, choice))

                    if choice == b"Stand":
                        state = DEALER_TURN
                    continue

                if state == DEALER_TURN:
                    v = card_value(rank)
                    dealer_total += v
                    if rank == 1:
                        dealer_aces += 1
                    dealer_total = adjust_for_aces(dealer_total, dealer_aces)

                    print(f"Dealer card: {card} (dealer total: {dealer_total})")

                    if result != 0:
                        final_result = result
                        state = DONE
                    continue

            if state == DONE:
                if final_result == 1:
                    print("✅ You WIN!")
                elif final_result == 2:
                    print("❌ You LOSE.")
                elif final_result == 3:
                    print("🤝 TIE.")
                else:
                    print(f"Round ended with unknown result code: {final_result}")

        except (ConnectionError, ValueError, RuntimeError) as e:
            print(f"Connection/game error: {e}")
            break
        except socket.timeout:
            print("Timeout waiting for server. Ending.")
            break

    tcp.close()
    print("\nDisconnected.")


if __name__ == "__main__":
    main()
