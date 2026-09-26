# src/events/management/commands/dogfood_import.py
"""F1 stub. Fixture format is decided here: a JSON array of records, top level.

The real importer is F3. This stub exists because entrypoint step 6 calls it on the
first boot, and a command that is merely absent stops that boot with `Unknown command`.
"""
import json
import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Seed the portal from a fixture file. F1: stub, counts and refuses."

    def add_arguments(self, parser):
        # All three arguments are fixed HERE, at F1, so that F3 changes this file and
        # nothing else. Entrypoint step 6 is the caller and it must never need editing.
        parser.add_argument("path")
        parser.add_argument("--if-empty", action="store_true",
                            help="seed only when the portal has no events yet")
        parser.add_argument("--dry-run", action="store_true",
                            help="report what would be imported; never exit non-zero")

    def handle(self, *args, **opts):
        path = opts["path"]
        if not os.path.isfile(path):
            # Exit 0, but say so in the boot log. Seeding is optional; a silent skip is
            # what section 15 fault 2 actually was, and the fix for it was visibility.
            self.stdout.write("import = no fixture at %s (nothing seeded)" % path)
            return
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            self.stdout.write("import = FIXTURE IS NOT A JSON ARRAY (%s)" % path)
            raise CommandError("%s must hold a JSON array of records" % path)
        if not data:
            self.stdout.write("import = skipped (0 records, --if-empty)"
                              if opts["if_empty"] else "import = skipped (0 records)")
            return
        self.stdout.write("import = NOT IMPLEMENTED (%d records at %s)" % (len(data), path))
        if opts["dry_run"]:
            return
        raise CommandError(
            "the F3 importer is not written yet and %d records were offered. "
            "Either run with --dry-run, or empty the fixture to boot without seeding." % len(data)
        )
