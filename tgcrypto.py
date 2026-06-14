import pyaes


def _as_bytes(value) -> bytes:
    return bytes(value)


def _xor(left: bytes, right: bytes) -> bytes:
    left = _as_bytes(left)
    right = _as_bytes(right)
    return bytes(a ^ b for a, b in zip(left, right))


def _check_args(data: bytes, key: bytes, iv: bytes):
    data = _as_bytes(data)
    key = _as_bytes(key)
    iv = _as_bytes(iv)

    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes.")
    if len(iv) != 32:
        raise ValueError("AES-IGE IV must be 32 bytes.")
    if len(data) % 16:
        raise ValueError("AES-IGE data length must be a multiple of 16 bytes.")

    return data, key, iv


def ige256_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    data, key, iv = _check_args(data, key, iv)

    aes = pyaes.AES(key)
    previous_cipher = iv[:16]
    previous_plain = iv[16:]
    result = bytearray()

    for offset in range(0, len(data), 16):
        plain = data[offset:offset + 16]
        cipher = _xor(aes.encrypt(_xor(plain, previous_cipher)), previous_plain)
        result.extend(cipher)
        previous_cipher = cipher
        previous_plain = plain

    return bytes(result)


def ige256_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    data, key, iv = _check_args(data, key, iv)

    aes = pyaes.AES(key)
    previous_cipher = iv[:16]
    previous_plain = iv[16:]
    result = bytearray()

    for offset in range(0, len(data), 16):
        cipher = data[offset:offset + 16]
        plain = _xor(aes.decrypt(_xor(cipher, previous_plain)), previous_cipher)
        result.extend(plain)
        previous_cipher = cipher
        previous_plain = plain

    return bytes(result)
