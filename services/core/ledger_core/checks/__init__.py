"""Postcondition check implementations, one module per channel.

Each module registers its checks with @postconditions.register(name).
postconditions.load_all() imports every module here, so adding a check never
requires editing a shared file. Owners: file.py (Track A), crm.py + email.py
(Track B), review.py (Track J), run.py (Track G).
"""
