import pytest
import os

def test_ac1_spelling_consistency():
    """
    Verify that 'behaviour' is not present in README.rst and docs/intro.rst, 
    and 'behavior' is present in both files.
    """
    files_to_check = ['README.rst', 'docs/intro.rst']
    
    for file_path in files_to_check:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            assert "behaviour" not in content, f"Found 'behaviour' in {file_path}"
            assert "behavior" in content, f"Did not find 'behavior' in {file_path}"
