import sys
import subprocess

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python -m database.upgrade_table_to_flattened_and_typed <sqlite_path> <table_name> [json_col] [id_col]")
        sys.exit(1)
    sqlite_path = sys.argv[1]
    table_name = sys.argv[2]
    json_col = sys.argv[3] if len(sys.argv) > 3 else 'json'
    id_col = sys.argv[4] if len(sys.argv) > 4 else 'id'

    # Both steps are invoked as `-m database.<module>`, not as file paths.
    # Running either file by path puts src/database/ on sys.path as a plain
    # directory rather than importing it as a package, so its `from .record_transformer`
    # import dies with "attempted relative import with no known parent package".
    # That is why this script used to fail at step 1 on every run.

    # Step 1: Flatten the table
    print(f"[1/2] Running convert_to_flattened on {table_name}...")
    subprocess.run([
        sys.executable, "-m", "database.convert_to_flattened",
        sqlite_path, table_name, json_col, id_col
    ], check=True)

    # Step 2: Fix legacy strings to add type info
    print(f"[2/2] Running fix_legacy_strings on {table_name}...")
    subprocess.run([
        sys.executable, "-m", "database.fix_legacy_strings",
        sqlite_path, table_name, id_col
    ], check=True)

    print("Upgrade complete. Table is now flattened and type-safe.")
