import sys
import os
import types

# Add the directory containing this script to sys.path so we can import proposed_encryption
dir_path = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, dir_path)

# Mock the app.services.encryption import structure so proposed_test_encryption can import it
app_mod = types.ModuleType("app")
app_services_mod = types.ModuleType("app.services")
sys.modules["app"] = app_mod
sys.modules["app.services"] = app_services_mod

import proposed_encryption
sys.modules["app.services.encryption"] = proposed_encryption

# Run pytest on the local proposed_test_encryption.py
import pytest

if __name__ == "__main__":
    test_file = os.path.join(dir_path, "proposed_test_encryption.py")
    print(f"Running tests on {test_file}...")
    sys.exit(pytest.main(["-v", test_file]))
