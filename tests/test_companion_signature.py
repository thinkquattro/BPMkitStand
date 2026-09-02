"""
Тесты проверки подписи канала обновлений: `standkit_companion.ed25519` и
`standkit_companion.signature`.

Зачем этот файл существует. Проверка подписи — единственное, что отделяет пользователя от
установки чужого исполняемого файла, и реализация у нас СВОЯ (репозиторий stdlib-only,
`cryptography`/`pynacl` взять нельзя). Своя криптография без тест-векторов — это код,
который «вроде работает»: почти любая ошибка в арифметике поля даёт реализацию, которая
успешно проверяет СВОИ подписи и молча принимает чужие. Поэтому здесь два независимых
источника истины:

* официальные тест-векторы RFC 8032 §7.1 — они ловят расхождение с остальным миром
  (неверная базовая точка, перепутанный порядок в SHA-512, big-endian вместо little);
* негативные случаи — порча бита, неверные длины, `S >= L` — они ловят ровно то, ради чего
  проверка и делается: подпись, которую принимать НЕЛЬЗЯ.

Почему `sign` живёт в тестах. Приватного ключа у клиента нет и быть не может, поэтому в
поставке `sign` отсутствует принципиально: готовый подписыватель рядом с открытым ключом
превращает утечку seed'а издателя в готовый инструмент подделки обновлений. Но без
подписывателя нельзя проверить `verify_artifact` на положительном пути, поэтому здесь
собран минимальный эталонный `_sign` по RFC 8032 §5.1.6 поверх примитивов `ed25519`.
Его собственная корректность проверяется тем же способом — воспроизведением векторов RFC
байт в байт (Ed25519 детерминирован, «почти совпало» невозможно).
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from standkit_companion import ed25519, signature as sigmod
from standkit_companion.errors import ChannelError

# --------------------------------------------------------------------------------------
# Официальные тест-векторы RFC 8032 §7.1 (Ed25519). Значения публичные, вбиты константами
# намеренно: вычислять их в тесте тем же кодом, который тестируем, — тавтология.
# Кортеж: (имя, seed/секретный ключ, публичный ключ, сообщение, подпись) — всё в hex.
# --------------------------------------------------------------------------------------
RFC8032_VECTORS = [
    (
        "TEST 1 (пустое сообщение)",
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "TEST 2 (1 байт)",
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "TEST 3 (2 байта)",
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
    (
        "TEST SHA(abc) (64 байта)",
        "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
        "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
        "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
        "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704",
    ),
]

# Seed, которым подписываются искусственные артефакты в тестах ниже. Это НЕ ключ издателя:
# ключ издателя приватным нам недоступен, да и не нужен — проверяется алгоритм, не ключ.
FAKE_SEED = bytes(range(32))
OTHER_SEED = bytes(range(100, 132))


# --------------------------------------------------------------------------------------
# Эталонный подписыватель (RFC 8032 §5.1.6) — ТОЛЬКО для тестов, см. докстринг модуля.
# --------------------------------------------------------------------------------------
def _secret_scalar_and_prefix(seed: bytes) -> tuple[int, bytes]:
    """Развернуть 32-байтовый seed в скаляр `a` и префикс детерминированного нонса.

    Обрезка битов (`&= 248`, `&= 127`, `|= 64`) — не украшение: младшие три бита обнуляются,
    чтобы скаляр был кратен кофактору 8, старший фиксируется, чтобы длина скаляра не текла
    по времени. Без неё подписи просто не совпадут с векторами RFC.
    """
    h = hashlib.sha512(seed).digest()
    scalar = bytearray(h[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return int.from_bytes(scalar, "little"), h[32:]


def _public_key(seed: bytes) -> bytes:
    """Публичный ключ из seed: A = [a]B в сжатом виде."""
    a, _prefix = _secret_scalar_and_prefix(seed)
    return ed25519._point_compress(ed25519._scalarmult_base(a))


def _sign(seed: bytes, message: bytes) -> bytes:
    """64 байта подписи Ed25519. Нонс детерминированный (SHA-512 от префикса и сообщения) —
    в Ed25519 нет источника случайности, и это часть стандарта, а не упрощение теста."""
    a, prefix = _secret_scalar_and_prefix(seed)
    pub = ed25519._point_compress(ed25519._scalarmult_base(a))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % ed25519.L
    r_point = ed25519._point_compress(ed25519._scalarmult_base(r))
    k = int.from_bytes(hashlib.sha512(r_point + pub + message).digest(), "little") % ed25519.L
    s = (r + k * a) % ed25519.L
    return r_point + s.to_bytes(32, "little")


def _flip_bit(data: bytes, index: int = 0, bit: int = 0) -> bytes:
    """Испортить ровно один бит — минимальная возможная порча. Если проверка её пропускает,
    она не проверяет ничего."""
    out = bytearray(data)
    out[index] ^= 1 << bit
    return bytes(out)


# ======================================================================================
# 1. Тест-векторы RFC 8032
# ======================================================================================
@pytest.mark.parametrize("name,seed_hex,pub_hex,msg_hex,sig_hex", RFC8032_VECTORS,
                         ids=[v[0] for v in RFC8032_VECTORS])
def test_rfc8032_vectors_verify(name, seed_hex, pub_hex, msg_hex, sig_hex):
    assert ed25519.verify(
        bytes.fromhex(pub_hex), bytes.fromhex(sig_hex), bytes.fromhex(msg_hex)
    ) is True, (
        f"Официальный вектор RFC 8032 §7.1 «{name}» не прошёл проверку. Это значит, что наша "
        f"реализация расходится со стандартом, а не что вектор плох: подписи издателя ей "
        f"проверять нельзя."
    )


@pytest.mark.parametrize("name,seed_hex,pub_hex,msg_hex,sig_hex", RFC8032_VECTORS,
                         ids=[v[0] for v in RFC8032_VECTORS])
def test_rfc8032_vectors_reproduced_by_test_signer(name, seed_hex, pub_hex, msg_hex, sig_hex):
    """Самопроверка эталонного `_sign` из этого файла.

    Ed25519 детерминирован: из того же seed и того же сообщения обязана получиться БАЙТ В
    БАЙТ та же подпись, что в RFC. Без этого теста положительные проверки `verify_artifact`
    ниже доказывали бы лишь то, что наш `sign` и наш `verify` согласованы между собой —
    даже если оба одинаково неправильны.
    """
    seed = bytes.fromhex(seed_hex)
    assert _public_key(seed).hex() == pub_hex, (
        f"«{name}»: публичный ключ, выведенный из seed, разошёлся с вектором RFC — сломана "
        f"либо обрезка битов скаляра, либо умножение базовой точки."
    )
    assert _sign(seed, bytes.fromhex(msg_hex)).hex() == sig_hex, (
        f"«{name}»: подпись, посчитанная тестовым подписывателем, разошлась с вектором RFC."
    )


# ======================================================================================
# 2. Негативные случаи: порча одного бита
# ======================================================================================
def test_single_bit_flip_in_signature_rejected():
    seed = FAKE_SEED
    pub = _public_key(seed)
    message = b"release payload digest"
    sig = _sign(seed, message)

    assert ed25519.verify(pub, sig, message) is True, "исходная подпись обязана проходить"

    for index in (0, 31, 32, 63):
        assert ed25519.verify(pub, _flip_bit(sig, index), message) is False, (
            f"Подпись с испорченным битом в байте {index} принята как валидная — проверка "
            f"не защищает ни от подмены точки R, ни от подмены скаляра S."
        )


def test_single_bit_flip_in_message_rejected():
    seed = FAKE_SEED
    pub = _public_key(seed)
    message = b"release payload digest"
    sig = _sign(seed, message)

    assert ed25519.verify(pub, sig, _flip_bit(message, 5)) is False, (
        "Подпись подтверждена для изменённого сообщения — значит, привязка подписи к "
        "содержимому не работает и подменить подписанный артефакт можно безнаказанно."
    )


def test_single_bit_flip_in_public_key_rejected():
    seed = FAKE_SEED
    pub = _public_key(seed)
    message = b"release payload digest"
    sig = _sign(seed, message)

    assert ed25519.verify(_flip_bit(pub, 1), sig, message) is False, (
        "Подпись подтверждена ЧУЖИМ публичным ключом — значит, ключ в проверке фактически "
        "не участвует."
    )


# ======================================================================================
# 3. Некорректные длины: False, но никогда исключение
# ======================================================================================
@pytest.mark.parametrize("key_len", [0, 31, 33, 64])
def test_wrong_public_key_length_returns_false_without_exception(key_len):
    seed = FAKE_SEED
    message = b"x"
    sig = _sign(seed, message)
    broken_key = (_public_key(seed) * 3)[:key_len]

    assert ed25519.verify(broken_key, sig, message) is False, (
        f"Ключ длиной {key_len} байт обязан давать False: на вход приходит содержимое файла "
        f"из сети, и падение с трейсбеком вместо отказа выглядит как баг канала."
    )


@pytest.mark.parametrize("sig_len", [0, 63, 65, 128])
def test_wrong_signature_length_returns_false_without_exception(sig_len):
    seed = FAKE_SEED
    message = b"x"
    pub = _public_key(seed)
    broken_sig = (_sign(seed, message) * 3)[:sig_len]

    assert ed25519.verify(pub, broken_sig, message) is False, (
        f"Подпись длиной {sig_len} байт обязана давать False без исключения."
    )


@pytest.mark.parametrize("bad", ["строка вместо байтов", 42, None, ["не", "байты"]])
def test_non_bytes_input_returns_false_without_exception(bad):
    seed = FAKE_SEED
    message = b"x"
    assert ed25519.verify(bad, _sign(seed, message), message) is False
    assert ed25519.verify(_public_key(seed), bad, message) is False
    assert ed25519.verify(_public_key(seed), _sign(seed, message), bad) is False


def test_undecodable_point_returns_false():
    """Ключ, который не является точкой кривой (все биты единицы), — отказ, не падение."""
    assert ed25519.verify(b"\xff" * 32, b"\x00" * 64, b"msg") is False


# ======================================================================================
# 4. Malleability: S >= L
# ======================================================================================
@pytest.mark.parametrize("offset", [0, 1, 2 ** 200])
def test_scalar_not_less_than_group_order_rejected(offset):
    """S должен быть строго меньше L.

    Без этой проверки к валидной подписи можно прибавить L и получить ВТОРУЮ подпись того же
    сообщения тем же ключом. Для канала обновлений это означало бы, что подпись перестаёт
    быть уникальным свидетельством о конкретном артефакте.
    """
    seed = FAKE_SEED
    pub = _public_key(seed)
    message = b"release payload digest"
    sig = _sign(seed, message)

    s_original = int.from_bytes(sig[32:], "little")
    forged_s = ed25519.L + offset
    forged = sig[:32] + forged_s.to_bytes(32, "little")

    assert s_original < ed25519.L, "подпись эталонного подписывателя обязана иметь S < L"
    assert ed25519.verify(pub, forged, message) is False, (
        f"Подпись с S = L + {offset} принята — проверка диапазона скаляра отсутствует, "
        f"подпись поддаётся размножению (malleability)."
    )


# ======================================================================================
# 5. decode_pubkey / load_pubkey_file
# ======================================================================================
def test_decode_pubkey_accepts_valid_raw_key():
    encoded = base64.b64encode(_public_key(FAKE_SEED)).decode("ascii")
    assert len(encoded) == 44 and encoded.endswith("="), (
        "44 символа с одним '=' — ровно та форма, в которой ключ лежит в файле поставки"
    )
    assert sigmod.decode_pubkey(encoded) == _public_key(FAKE_SEED)


def test_decode_pubkey_accepts_vendored_license_key():
    """Вшитый ключ ЛИЦЕНЗИЙ обязан быть разбираемым — иначе проверка отзыва мертва с рождения."""
    raw = sigmod.decode_pubkey(sigmod.PUBLISHER_LICENSE_PUBKEY_B64)
    assert len(raw) == sigmod.RAW_KEY_LEN


@pytest.mark.parametrize("value,why", [
    ("", "пустая строка"),
    ("   \n  ", "только пробелы"),
    ("# ключ ещё не сгенерирован", "строка-комментарий"),
    ("ключ ещё не сгенерирован", "плейсхолдер русским текстом"),
    ("REPLACE_ME", "плейсхолдер латиницей"),
    (base64.b64encode(b"\x01" * 31).decode("ascii"), "base64 от 31 байта"),
    (base64.b64encode(b"\x01" * 33).decode("ascii"), "base64 от 33 байт"),
    (None, "вообще не строка"),
])
def test_decode_pubkey_rejects_placeholders(value, why):
    with pytest.raises(ChannelError) as excinfo:
        sigmod.decode_pubkey(value)
    assert excinfo.value.kind == "pubkey_missing", (
        f"Случай «{why}» обязан давать kind='pubkey_missing' (канал релизов не настроен), "
        f"а не 'artifact_signature_invalid': пользователю нужно выпустить ключ, а не искать "
        f"подмену файла. Получено: {excinfo.value.kind}"
    )


def test_load_pubkey_file_reads_single_line(tmp_path):
    key_file = tmp_path / "artifacts.pub"
    key_file.write_text(
        base64.b64encode(_public_key(FAKE_SEED)).decode("ascii") + "\n", encoding="utf-8"
    )
    assert sigmod.load_pubkey_file(key_file) == _public_key(FAKE_SEED)


def test_load_pubkey_file_missing_file_is_pubkey_missing(tmp_path):
    with pytest.raises(ChannelError) as excinfo:
        sigmod.load_pubkey_file(tmp_path / "нет-такого-файла.pub")
    assert excinfo.value.kind == "pubkey_missing", (
        "Отсутствие файла ключа — такой же штатный исход «ключ ещё не выпущен», как и "
        "плейсхолдер внутри него"
    )


# ======================================================================================
# 6. verify_artifact
# ======================================================================================
ARTIFACT_NAME = "bpmkit-1.2.3.exe"
ARTIFACT_BODY = b"MZ\x90\x00" + b"artifact payload " * 64


@pytest.fixture
def artifact(tmp_path):
    """Настоящий файл на диске + честно подписанный сайдкар к нему.

    tmp_path вместо моков сознательно: `verify_artifact` читает размер и sha256 с диска, и
    подмена файловых операций мок-объектом проверяла бы моки, а не проверку.
    """
    path = tmp_path / ARTIFACT_NAME
    path.write_bytes(ARTIFACT_BODY)

    pub = _public_key(FAKE_SEED)
    digest = hashlib.sha256(ARTIFACT_BODY).digest()
    # Подписывается РОВНО сырой 32-байтовый дайджест — не файл, не hex-строка, не JSON.
    raw_sig = _sign(FAKE_SEED, digest)

    sidecar = {
        "format": sigmod.SIG_FORMAT,
        "artifact": ARTIFACT_NAME,
        "size": len(ARTIFACT_BODY),
        "sha256": digest.hex(),
        "signed_at": "2026-08-20T15:36:00Z",
        "key_id": hashlib.sha256(pub).hexdigest()[:16],
        # Стандартный base64, НЕ base64url — так его пишет издатель.
        "signature": base64.b64encode(raw_sig).decode("ascii"),
    }
    return path, sidecar, pub


def test_verify_artifact_happy_path(artifact):
    path, sidecar, pub = artifact
    result = sigmod.verify_artifact(
        path, sidecar, pub,
        expected_name=ARTIFACT_NAME,
        expected_sha256=hashlib.sha256(ARTIFACT_BODY).hexdigest(),
    )
    assert result["key_id"] == sidecar["key_id"]
    assert result["signed_at"] == "2026-08-20T15:36:00Z"
    assert result["sha256"] == hashlib.sha256(ARTIFACT_BODY).hexdigest()
    assert result["size"] == len(ARTIFACT_BODY)


def test_verify_artifact_accepts_uppercase_sha256(artifact):
    """Регистр hex-дайджеста не значим: издатель пишет нижний, но сравнение обязано быть
    регистронезависимым — иначе смена генератора сайдкаров положит канал целиком."""
    path, sidecar, pub = artifact
    sidecar["sha256"] = sidecar["sha256"].upper()
    assert sigmod.verify_artifact(path, sidecar, pub, expected_name=ARTIFACT_NAME)


@pytest.mark.parametrize("mutate,kind,why", [
    (lambda s: s.pop("signature"), "signature_not_available", "сайдкар без обязательного поля"),
    (lambda s: s.update(format="bpmkit-artifact-sig-v2"), "signature_not_available",
     "неизвестный формат сайдкара"),
    (lambda s: s.update(artifact="bpmkit-9.9.9.exe"), "signature_not_available",
     "сайдкар выписан на другой файл (защита от отката на старый подписанный релиз)"),
    (lambda s: s.update(size=s["size"] + 1), "integrity_mismatch", "не сошёлся размер"),
    (lambda s: s.update(sha256="0" * 64), "integrity_mismatch", "не сошёлся sha256"),
    (lambda s: s.update(key_id="deadbeefdeadbeef"), "artifact_signature_invalid",
     "сайдкар подписан чужим ключом"),
])
def test_verify_artifact_failure_kinds(artifact, mutate, kind, why):
    path, sidecar, pub = artifact
    mutate(sidecar)
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, sidecar, pub, expected_name=ARTIFACT_NAME)
    assert excinfo.value.kind == kind, (
        f"Случай «{why}» обязан давать kind={kind!r}: от этого зависит и текст для "
        f"пользователя, и решение «повторять ли на следующем тике». Получено "
        f"{excinfo.value.kind!r}."
    )


def test_verify_artifact_not_dict_is_signature_not_available(artifact):
    path, _sidecar, pub = artifact
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, None, pub)
    assert excinfo.value.kind == "signature_not_available"


def test_verify_artifact_corrupted_signature(artifact):
    path, sidecar, pub = artifact
    raw = bytearray(base64.b64decode(sidecar["signature"]))
    raw[40] ^= 0x01
    sidecar["signature"] = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, sidecar, pub, expected_name=ARTIFACT_NAME)
    assert excinfo.value.kind == "artifact_signature_invalid", (
        "Испорченная подпись при сошедшемся sha256 — это не проблема целостности, а "
        "недействительная подпись: повторная закачка её не починит, и ретраить бессмысленно"
    )


def test_verify_artifact_signature_by_wrong_key(artifact):
    """Сайдкар с правильным key_id, но подписью ОТ ДРУГОГО ключа — худший из реальных
    случаев: метаданные выглядят своими, подделана только криптография."""
    path, sidecar, pub = artifact
    digest = hashlib.sha256(ARTIFACT_BODY).digest()
    sidecar["signature"] = base64.b64encode(_sign(OTHER_SEED, digest)).decode("ascii")

    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, sidecar, pub, expected_name=ARTIFACT_NAME)
    assert excinfo.value.kind == "artifact_signature_invalid"


def test_verify_artifact_body_tampered_after_signing(artifact):
    """Файл подменён, сайдкар подлинный — обязан ловиться на sha256 ДО криптографии."""
    path, sidecar, pub = artifact
    path.write_bytes(ARTIFACT_BODY.replace(b"payload", b"pAyload"))

    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, sidecar, pub, expected_name=ARTIFACT_NAME)
    assert excinfo.value.kind == "integrity_mismatch"


def test_verify_artifact_expected_sha256_mismatch(artifact):
    """Сайдкар и метаданные релиза приезжают разными ответами; их расхождение — отдельный
    сигнал, а не дубль проверки sha256."""
    path, sidecar, pub = artifact
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, sidecar, pub,
                               expected_name=ARTIFACT_NAME, expected_sha256="1" * 64)
    assert excinfo.value.kind == "integrity_mismatch"


def test_verify_artifact_missing_file_is_integrity_mismatch(tmp_path, artifact):
    path, sidecar, pub = artifact
    path.unlink()
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, sidecar, pub, expected_name=ARTIFACT_NAME)
    assert excinfo.value.kind == "integrity_mismatch"


def test_verify_artifact_with_unusable_pubkey(artifact):
    path, sidecar, _pub = artifact
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, sidecar, b"\x00" * 31, expected_name=ARTIFACT_NAME)
    assert excinfo.value.kind == "pubkey_missing"


def test_verify_artifact_error_messages_are_specific(artifact):
    """Сообщение обязано содержать и ожидаемое, и фактическое значение: «контрольная сумма
    не сошлась» без чисел не даёт владельцу ничего."""
    path, sidecar, pub = artifact
    sidecar["size"] = 1
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_artifact(path, sidecar, pub, expected_name=ARTIFACT_NAME)
    text = str(excinfo.value)
    assert "1" in text and str(len(ARTIFACT_BODY)) in text


# ======================================================================================
# 7. verify_revocations_document
# ======================================================================================
def _sign_revocations(doc: dict, seed: bytes = FAKE_SEED) -> dict:
    """Подписать документ отзыва так же, как это делает оффлайн-подписыватель издателя:
    сырые байты канонического JSON БЕЗ поля signature, без промежуточного хеша."""
    payload = json.dumps(
        {k: v for k, v in doc.items() if k != "signature"},
        separators=(",", ":"), sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")
    signed = dict(doc)
    # base64url без padding — ровно та форма, в которой подпись лежит в revocations.json.
    signed["signature"] = base64.urlsafe_b64encode(_sign(seed, payload)).decode("ascii").rstrip("=")
    return signed


def test_verify_revocations_happy_path():
    doc = _sign_revocations({
        "v": 1,
        "generated_at": "2026-08-20T12:00:00Z",
        "revoked": ["lic-0001", "lic-0002"],
    })
    assert sigmod.verify_revocations_document(doc, _public_key(FAKE_SEED)) == \
        ["lic-0001", "lic-0002"]


def test_verify_revocations_uses_vendored_license_key_by_default():
    """Без явного ключа берётся вшитый ключ ЛИЦЕНЗИЙ, а не ключ артефактов. Документ,
    подписанный посторонним ключом, обязан быть отвергнут именно поэтому."""
    doc = _sign_revocations({"v": 1, "revoked": []})
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_revocations_document(doc)
    assert excinfo.value.kind == "revocations_signature_invalid"


OTHER_SEED = bytes(range(32, 64))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def test_revocation_pubkey_constant_is_raw_key_distinct_from_license_key():
    """BE-02: вшитый ключ канала отзыва — валидный сырой Ed25519-ключ и НЕ совпадает с
    лицензионным (разделение ключей — смысл всей правки)."""
    rev = sigmod.decode_pubkey(sigmod.PUBLISHER_REVOCATION_PUBKEY_B64)
    lic = sigmod.decode_pubkey(sigmod.PUBLISHER_LICENSE_PUBKEY_B64)
    assert len(rev) == sigmod.RAW_KEY_LEN and len(lic) == sigmod.RAW_KEY_LEN
    assert rev != lic


def test_verify_revocations_default_accepts_revocation_channel_key(monkeypatch):
    """BE-02 (аудит 01.09.2026): документ, подписанный ключом КАНАЛА ОТЗЫВА (так подписывает
    сервер), обязан приниматься без явного pubkey_raw. До правки companion пробовал только
    лицензионный ключ и отбрасывал КАЖДЫЙ серверный документ."""
    monkeypatch.setattr(sigmod, "PUBLISHER_REVOCATION_PUBKEY_B64", _b64(_public_key(FAKE_SEED)))
    monkeypatch.setattr(sigmod, "PUBLISHER_LICENSE_PUBKEY_B64", _b64(_public_key(OTHER_SEED)))
    doc = _sign_revocations({"v": 1, "generated_at": "2026-09-01T00:00:00Z",
                             "revoked": ["lic-0009"]}, seed=FAKE_SEED)
    assert sigmod.verify_revocations_document(doc) == ["lic-0009"]


def test_verify_revocations_default_falls_back_to_license_key(monkeypatch):
    """Обратная совместимость: документ, подписанный лицензионным ключом (исторический канал),
    по-прежнему принимается вторым кандидатом."""
    monkeypatch.setattr(sigmod, "PUBLISHER_REVOCATION_PUBKEY_B64", _b64(_public_key(OTHER_SEED)))
    monkeypatch.setattr(sigmod, "PUBLISHER_LICENSE_PUBKEY_B64", _b64(_public_key(FAKE_SEED)))
    doc = _sign_revocations({"v": 1, "revoked": ["lic-0001"]}, seed=FAKE_SEED)
    assert sigmod.verify_revocations_document(doc) == ["lic-0001"]


def test_verify_revocations_default_rejects_foreign_key(monkeypatch):
    """Документ, подписанный третьим ключом, не проходит ни по одному из двух кандидатов."""
    monkeypatch.setattr(sigmod, "PUBLISHER_REVOCATION_PUBKEY_B64", _b64(_public_key(OTHER_SEED)))
    monkeypatch.setattr(sigmod, "PUBLISHER_LICENSE_PUBKEY_B64", _b64(_public_key(bytes(range(64, 96)))))
    doc = _sign_revocations({"v": 1, "revoked": ["lic-0001"]}, seed=FAKE_SEED)
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_revocations_document(doc)
    assert excinfo.value.kind == "revocations_signature_invalid"


def test_verify_revocations_explicit_key_has_no_fallback(monkeypatch):
    """Явный pubkey_raw — ровно один ключ: вшитые кандидаты НЕ подмешиваются."""
    monkeypatch.setattr(sigmod, "PUBLISHER_REVOCATION_PUBKEY_B64", _b64(_public_key(FAKE_SEED)))
    doc = _sign_revocations({"v": 1, "revoked": ["lic-0001"]}, seed=FAKE_SEED)
    with pytest.raises(ChannelError):
        sigmod.verify_revocations_document(doc, _public_key(OTHER_SEED))


def test_verify_revocations_missing_signature():
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_revocations_document(
            {"v": 1, "generated_at": "2026-08-20T12:00:00Z", "revoked": ["lic-0001"]},
            _public_key(FAKE_SEED),
        )
    assert excinfo.value.kind == "revocations_signature_invalid", (
        "Неподписанный документ отзыва НЕ должен трактоваться как пустой список: подделка "
        "документа означает снятие отзыва с отозванной лицензии"
    )


def test_verify_revocations_tampered_after_signing():
    """Классическая атака: убрать свой id из списка отозванных, подпись оставить старую."""
    doc = _sign_revocations({
        "v": 1,
        "generated_at": "2026-08-20T12:00:00Z",
        "revoked": ["lic-0001", "lic-0002"],
    })
    doc["revoked"] = ["lic-0001"]

    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_revocations_document(doc, _public_key(FAKE_SEED))
    assert excinfo.value.kind == "revocations_signature_invalid"


def test_verify_revocations_extra_field_after_signing():
    doc = _sign_revocations({"v": 1, "revoked": ["lic-0001"]})
    doc["note"] = "добавлено после подписания"
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_revocations_document(doc, _public_key(FAKE_SEED))
    assert excinfo.value.kind == "revocations_signature_invalid"


def test_verify_revocations_signature_garbage():
    doc = {"v": 1, "revoked": [], "signature": "не-подпись-а-текст"}
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_revocations_document(doc, _public_key(FAKE_SEED))
    assert excinfo.value.kind == "revocations_signature_invalid"


def test_verify_revocations_canonicalization_keeps_cyrillic_as_utf8():
    """Защита от `ensure_ascii=True`.

    С `ensure_ascii=True` кириллица сериализуется как `\\uXXXX`, и байты расходятся с тем,
    что подписал издатель. Коварство в том, что документ БЕЗ кириллицы при этом проверялся
    бы нормально: баг вылез бы только в проде и только на части документов.
    """
    doc = _sign_revocations({
        "v": 1,
        "generated_at": "2026-08-20T12:00:00Z",
        "reason": "Отозвано по требованию правообладателя",
        "revoked": ["lic-кириллица-0001"],
    })

    payload = sigmod.canonical_revocations_payload(doc)
    assert "Отозвано".encode("utf-8") in payload, (
        "Каноническая сериализация обязана содержать кириллицу в UTF-8, а не в виде "
        "\\uXXXX — иначе подпись издателя не сойдётся на любом документе с русским текстом"
    )
    assert sigmod.verify_revocations_document(doc, _public_key(FAKE_SEED)) == \
        ["lic-кириллица-0001"]


def test_canonical_payload_is_key_order_independent():
    """`sort_keys=True` обязателен: порядок ключей в JSON не определён, и без сортировки
    подпись сходилась бы через раз в зависимости от того, кто собрал словарь."""
    first = {"v": 1, "generated_at": "2026-08-20T12:00:00Z", "revoked": ["a"]}
    second = {"revoked": ["a"], "v": 1, "generated_at": "2026-08-20T12:00:00Z"}
    assert sigmod.canonical_revocations_payload(first) == \
        sigmod.canonical_revocations_payload(second)


def test_canonical_payload_has_no_whitespace():
    """`separators=(",", ":")`: любой пробел меняет байты, а значит и подпись."""
    payload = sigmod.canonical_revocations_payload({"v": 1, "revoked": ["a", "b"]})
    assert b" " not in payload and b"\n" not in payload


def test_verify_revocations_not_a_dict():
    with pytest.raises(ChannelError) as excinfo:
        sigmod.verify_revocations_document(["не", "документ"], _public_key(FAKE_SEED))
    assert excinfo.value.kind == "revocations_signature_invalid"
