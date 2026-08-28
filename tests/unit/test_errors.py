import pytest

from jarvis.core import errors

_ALL_ERROR_CLASSES = [
    obj
    for obj in vars(errors).values()
    if isinstance(obj, type) and issubclass(obj, Exception) and obj is not Exception
]


def test_all_errors_derive_from_jarvis_error():
    assert _ALL_ERROR_CLASSES, "expected at least one error class in jarvis.core.errors"
    for cls in _ALL_ERROR_CLASSES:
        assert issubclass(cls, errors.JarvisError)


@pytest.mark.parametrize(
    "cls, expected_code",
    [
        (errors.UserError, 1),
        (errors.IntegrityError, 2),
        (errors.GateNotMetError, 3),
    ],
)
def test_exit_codes(cls, expected_code):
    assert cls.exit_code == expected_code
