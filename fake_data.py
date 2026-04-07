"""
fake_data.py
------------
Generate realistic fake personal data using Faker.
"""

from __future__ import annotations

from faker import Faker


def generate_fake_data() -> dict[str, str]:
    """
    Generate one realistic fake profile.

    Returns
    -------
    dict[str, str]
        Dictionary containing name, email, phone, and address.
    """
    # Create Faker instance with a broad locale set for more natural-looking data.
    fake = Faker(["en_US", "vi_VN"])

    # Build fake profile fields used in forms and testing.
    full_name = fake.name()
    email = fake.email()
    phone = fake.phone_number()
    address = fake.address().replace("\n", ", ")

    return {
        "name": full_name,
        "email": email,
        "phone": phone,
        "address": address,
    }


if __name__ == "__main__":
    # Quick manual test: print one generated record.
    print(generate_fake_data())
