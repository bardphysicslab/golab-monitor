# Hardware file locations

BardBox uses `hardware/ecad` for KiCad electronics and `hardware/mcad` for
mechanical/enclosure CAD, following RKC Monitor and the project template. Preserve
revisioned source and fabrication exports together with traceable references.

These directories establish hardware locations only. Existing application and
firmware entry points are preserved; no production migration is included.
Firmware configuration examples should be named `config.example.h`, with private
local `config.h` ignored. Do not overwrite existing private values during migration.
