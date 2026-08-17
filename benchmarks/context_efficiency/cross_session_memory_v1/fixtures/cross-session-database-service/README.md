# Database service fixture

This small service keeps its migration entry under `migrations/env.py` and its
runtime database setup under `src/database.py`. Compatibility helpers live in
`src/mysql_compat.py`. The repository intentionally contains no architecture
decision record.
