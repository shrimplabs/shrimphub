EDITING EXISTING FILES: Use patch_file for targeted changes — do NOT rewrite the entire file
via write_file. patch_file does exact string replacement and fails clearly if the text isn't found.
Example: patch_file("path/to/file.ext", "exact old text to find", "new replacement text")
Always read_file first so you have the exact text to match.
