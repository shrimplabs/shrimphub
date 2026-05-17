SCRATCHPAD (in-memory notes, survive context compaction, cleared when agent exits):
- scratchpad_write(type, content, files, key): Save a note. type="observation"|"plan"|"gotcha"|"progress". files=[] of related paths. key= optional name for direct retrieval.
- scratchpad_read(type, files, key): Read notes back. Filter by type, files, or key. No args = all notes.
Call scratchpad_read() at the start of every session in case context was compacted. Write a note any time you discover a gotcha, make a plan, or want to track partial progress across many loops.
Examples:
  scratchpad_write(type="plan", content="1. Add endpoint\n2. Write test\n3. Run pytest\n4. Commit")
  scratchpad_write(type="gotcha", content="patch_file requires exact match — read the file first to copy the exact text", files=["src/module.py"])
  scratchpad_read(type="gotcha")
