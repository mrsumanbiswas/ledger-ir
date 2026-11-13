"""GSTIN checksum/validation contract (SPEC.md §1.3)."""
from functions.ast_compiler.core import gstin

SUPPLIER = "24ABCDE1234F1Z6"
BUYER = "32FGHIJ5678K1ZA"


def test_known_check_digits():
    # verified against real-format GSTINs quoted in SPEC.md §1.3
    assert gstin.gstin_check("27AAPFU0939F1ZV") == "V"
    assert gstin.gstin_check("29AAGCB7383J1Z4") == "4"


def test_fixture_gstins_are_valid():
    assert gstin.is_valid_gstin(SUPPLIER)
    assert gstin.is_valid_gstin(BUYER)


def test_corrupted_or_malformed_rejected():
    assert not gstin.is_valid_gstin("24ABCDE1234F1Z7")   # wrong check char
    assert not gstin.is_valid_gstin("24ABCDE1234F1Z")    # too short
    assert not gstin.is_valid_gstin("24abcde1234f1z6")   # lowercase fails structure
    assert not gstin.is_valid_gstin("2AABCDE1234F1Z6")   # non-numeric state code


def test_state_code():
    assert gstin.state_code(SUPPLIER) == "24"
    assert gstin.state_code(BUYER) == "32"
