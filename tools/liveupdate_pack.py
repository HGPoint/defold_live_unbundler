import argparse
import fnmatch
import hashlib
import json
import math
import os
import sys
import time
import zipfile

import liveupdate_ddf_pb2

MAX_ARCHIVE_SIZE = 7340032
HASH_LEN = 16

# Hosting rejects files over 25 MiB.
MIB = 1024 * 1024
ZIP_STORE_ARCHIVE_LIMIT = 24 * MIB
HOSTING_FILE_LIMIT = 25 * MIB

MERGE_SMALL_BUDGET = HOSTING_FILE_LIMIT
MD5_CHUNK_SIZE = 256 * 1024
PACK_PREFIX = "pack_"
MERGE_RULE_KEYS = ("enabled", "small_archive_kib", "pack_limit_kib", "textures")
MERGE_KEYS = ("enabled", "modules_lua", "module_patterns", "small_archive_kib", "pack_limit_kib", "textures", "modules", "packs", "exclude")

# Fixed timestamp for every zip entry. Build tools (e.g. bob) may stamp extracted
# resources with epoch 0 (1970), which the ZIP format cannot represent — it only
# supports dates >= 1980 — and zipfile would raise "ZIP does not support
# timestamps before 1980" when writing them. A constant also makes the archives
# byte-reproducible (independent of source mtimes).
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# Postfix appended to a collection's id to form its archive/manifest name. The
# build graph delivers collections as "<id>.collectionc"; the last extension is
# stripped and this suffix is put in its place. Default is empty (no postfix);
# a project enables it via the LIVEUPDATE_COLLECTION_SUFFIX env var (this repo's
# Makefile sets ".collectionc", which reproduces os.path.basename byte-for-byte).
COLLECTION_SUFFIX = os.environ.get("LIVEUPDATE_COLLECTION_SUFFIX", "")

# Must match Bob's MurmurHash.hash64: release manifests keep url_hash but clear url.
_MURMUR64_M = 0xC6A4A7935BD1E995
_MURMUR64_R = 47
_MURMUR64_MASK = (1 << 64) - 1


def path_url_hash(path):
    data = path.encode("utf-8") if isinstance(path, str) else path
    h = 0
    nblocks = len(data) // 8
    for i in range(nblocks):
        k = int.from_bytes(data[i * 8 : (i + 1) * 8], "little")
        k = (k * _MURMUR64_M) & _MURMUR64_MASK
        k ^= k >> _MURMUR64_R
        k = (k * _MURMUR64_M) & _MURMUR64_MASK
        h = (h * _MURMUR64_M) & _MURMUR64_MASK
        h ^= k
    rem = len(data) % 8
    off = nblocks * 8
    k = 0
    if rem >= 7:
        k ^= data[off + 6] << 48
    if rem >= 6:
        k ^= data[off + 5] << 40
    if rem >= 5:
        k ^= data[off + 4] << 32
    if rem >= 4:
        k ^= data[off + 3] << 24
    if rem >= 3:
        k ^= data[off + 2] << 16
    if rem >= 2:
        k ^= data[off + 1] << 8
    if rem >= 1:
        k ^= data[off]
    if rem:
        k = (k * _MURMUR64_M) & _MURMUR64_MASK
        k ^= k >> _MURMUR64_R
        k = (k * _MURMUR64_M) & _MURMUR64_MASK
        h = (h * _MURMUR64_M) & _MURMUR64_MASK
        h ^= k
    k = len(data)
    k = (k * _MURMUR64_M) & _MURMUR64_MASK
    k ^= k >> _MURMUR64_R
    k = (k * _MURMUR64_M) & _MURMUR64_MASK
    h = (h * _MURMUR64_M) & _MURMUR64_MASK
    h ^= k
    h ^= h >> _MURMUR64_R
    h = (h * _MURMUR64_M) & _MURMUR64_MASK
    h ^= h >> _MURMUR64_R
    return h


class PackContext:
    def __init__(
        self,
        restore_from_tree=False,
        client_version=None,
        zip_store=False,
        merge_config=None,
    ):
        self.restore_from_tree = restore_from_tree
        self.client_version = client_version
        self.zip_store = zip_store
        self.merge_config = merge_config
        print("restore_from_tree: ", self.restore_from_tree)
        if self.zip_store:
            print("zip_store: 24 MiB store, then deflate, reject over 25 MiB")
        if self.merge_config:
            print(
                f"merge: archives under {self.merge_config['small_archive_kib']} KiB into {PACK_PREFIX}* "
                f"per client module, up to {self.merge_config['pack_limit_kib']} KiB"
            )
        else:
            print("merge: off, one archive per collection")

        self.debug_files = False
        self.graph_path = "build/default/game.graph.json"
        self.resources_folder = "liveupdate_dist/"
        self.result_folder = "liveupdate_zip/"
        self.current_directory = os.getcwd()
        self.dmanifest_name = "liveupdate.game.dmanifest"
        self.dmanifest_path = os.path.join(
            self.current_directory, self.resources_folder, self.dmanifest_name
        )
        self.extension_archive = ".arcd0"
        self.current_timestamp = str(math.floor(time.time()))
        self.temp_suffix = self.current_timestamp

        self.added_files = {}
        self.created_archives = {}
        self.archive_files = {}
        self.files_tree = {"zip_files": {}}
        self.merged_archives = {}

        self.dmanifest = None
        self.dmanifest_data = None
        self.files = {}
        self.excluded_proxies = {}
        self.zip_files = {}
        self.common_files = {}
        self.resources_by_url = {}
        self.resources_by_url_hash = {}
        self.resources_by_hash = {}
        self.dependency_list = {}

    def get_file_size(self, hex_digest):
        full_path = os.path.join(
            self.current_directory, self.resources_folder, hex_digest
        )
        if os.path.exists(full_path):
            return os.path.getsize(full_path)

    def load_json_file(self, file_path):
        try:
            with open(file_path, "r") as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise Exception(f"Error loading JSON from {file_path}: {e}")

    def parse_protobuf_file(self, file_path, proto_class):
        try:
            with open(file_path, "rb") as file:
                proto_instance = proto_class()
                proto_instance.ParseFromString(file.read())
                return proto_instance
        except FileNotFoundError:
            raise Exception(f"Protobuf file not found: {file_path}")
        except Exception as e:
            raise Exception(f"Error parsing protobuf file {file_path}: {e}")

    def precheck_all_files_for_list(self, files_list):
        missing_files = []
        for filepath in files_list:
            file_info = self.files.get(filepath)
            if not file_info:
                print(f"Warning: No file info for {filepath}")
                continue
            hex_digest = file_info.get("hexDigest")
            if not hex_digest:
                missing_files.append(filepath)
        if missing_files:
            print("\nFiles missing hexDigest:")
            header = "{:<6} {:<30} {:<}".format("No.", "Reason", "Path")
            print(header)
            print("-" * len(header))
            for i, path in enumerate(missing_files, start=1):
                print("{:<6} {:<30} {:<}".format(i, "Missing hexDigest", path))
            raise Exception("Found files missing hexDigest, build aborted.")

    def entry_compression(self, zip_file, data):
        if not self.zip_store:
            return zipfile.ZIP_DEFLATED
        size = len(data.encode("utf-8") if isinstance(data, str) else data)
        written = getattr(zip_file, "_liveupdate_written", 0)
        if written + size <= ZIP_STORE_ARCHIVE_LIMIT:
            return zipfile.ZIP_STORED
        return zipfile.ZIP_DEFLATED

    def record_written(self, zip_file):
        if not self.zip_store:
            return
        written = getattr(zip_file, "_liveupdate_written", 0)
        zip_file._liveupdate_written = written + zip_file.filelist[-1].compress_size

    def reject_oversized_archive(self, zip_path):
        if not self.zip_store:
            return
        size = os.path.getsize(zip_path)
        if size > HOSTING_FILE_LIMIT:
            sys.exit(
                f"[liveupdate] {zip_path} is {size / 1048576:.2f} MiB, over the "
                f"{HOSTING_FILE_LIMIT / 1048576:.0f} MiB hosting limit even with deflate; shrink its atlas"
            )

    def add_files_to_zip(
        self,
        common_zip_name,
        zip_file,
        files_list,
        dmanifest_data,
        zip_name,
        content_hashers,
        resources_list,
        common_files_list_name=None,
    ):
        seen_entries = set()
        for filepath in files_list:
            file_info = self.files.get(filepath)
            if not file_info:
                raise Exception(f"File info not found for {filepath}")
            hex_digest = file_info.get("hexDigest")
            size = file_info.get("size")
            if not size:
                raise Exception(f"Missing size for file: {filepath}")

            if not hex_digest:
                raise Exception(f"Missing hexDigest for file: {filepath}")

            file_path_hex = os.path.join(
                self.current_directory,
                self.resources_folder,
                self.files[filepath]["hexDigest"],
            )
            archive_info = self.files_tree["zip_files"].setdefault(
                common_zip_name, {"files": [], "size": 0}
            )
            archive_info["files"].append(file_info)
            if hex_digest not in self.added_files:
                archive_info["size"] += size
                with open(file_path_hex, "rb") as file_obj:
                    file_contents = file_obj.read()
                    content_hashers["all"].update(file_contents)
                    content_hashers["no_manifest"].update(file_contents)
                    # write the already-read bytes with a fixed timestamp instead of
                    # zip_file.write(path), which would read the source file's mtime
                    # (epoch 0 from bob -> pre-1980 ZIP error)
                    zinfo = zipfile.ZipInfo(
                        self.files[filepath]["hexDigest"], date_time=ZIP_EPOCH
                    )
                    zinfo.compress_type = self.entry_compression(zip_file, file_contents)
                    zip_file.writestr(zinfo, file_contents)
                    self.record_written(zip_file)

                    self.added_files[self.files[filepath]["hexDigest"]] = zip_name

            # Only this archive's paths: a foreign alias of the same digest would change version.
            for resource_entry in self.entries_for_path(filepath):
                key = (resource_entry.url, resource_entry.url_hash)
                if key in seen_entries:
                    continue
                seen_entries.add(key)
                dmanifest_data.resources.append(resource_entry)
                resources_list.append(resource_entry)

            if common_files_list_name:
                for main_file in self.common_files.get(filepath, {}).get("files", []):
                    if main_file not in self.dependency_list:
                        self.dependency_list[main_file] = []
                    if common_files_list_name not in self.dependency_list[main_file]:
                        self.dependency_list[main_file].append(common_files_list_name)

    def create_zip_archive(self, zip_name, files_list, common_files_list_name=None):
        # Layout planning shares hashes across archives; keep a local guard for
        # aliases inside an archive and callers replaying a layout directly.
        self.added_files = {}
        common_zip_name = zip_name
        zip_name = zip_name + self.temp_suffix
        zip_path = os.path.join(self.result_folder, zip_name + self.extension_archive)
        self.dmanifest_data.ClearField("resources")
        self.files_tree["zip_files"].setdefault(common_zip_name, {"files": [], "size": 0})
        content_hashers = {
            "all": hashlib.sha256(),
            "no_manifest": hashlib.sha256(),
            "dmanifest": hashlib.sha256(),
        }
        resources_list = []
        print(f"Creating archive: {common_zip_name}{self.extension_archive}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            self.add_files_to_zip(
                common_zip_name,
                zip_file,
                files_list,
                self.dmanifest_data,
                zip_name,
                content_hashers,
                resources_list,
                common_files_list_name,
            )

            new_dmanifest_data = self.dmanifest_data.SerializeToString()
            self.dmanifest.data = new_dmanifest_data
            dmanifest_bytes = self.dmanifest.SerializeToString()
            content_hashers["dmanifest"].update(dmanifest_bytes)
            content_hashers["all"].update(dmanifest_bytes)
            dmanifest_info = zipfile.ZipInfo(self.dmanifest_name, date_time=ZIP_EPOCH)
            dmanifest_info.compress_type = self.entry_compression(zip_file, dmanifest_bytes)
            zip_file.writestr(dmanifest_info, dmanifest_bytes)
            self.record_written(zip_file)
        self.reject_oversized_archive(zip_path)
        content_hash_no_manifest = content_hashers["no_manifest"].hexdigest()
        version_hasher = hashlib.sha256()
        for resource_entry in sorted(
            resources_list, key=lambda item: (item.hash.data.hex(), item.url)
        ):
            version_hasher.update(resource_entry.SerializeToString())
        version_hasher.update(b"content_hash_no_manifest:")
        version_hasher.update(content_hash_no_manifest.encode("utf-8"))
        version_hash = self.truncate_hash(version_hasher.hexdigest())
        self.created_archives[common_zip_name] = {
            "path": zip_path,
            "version_hash": version_hash,
            "size": os.path.getsize(zip_path),
        }
        (
            self.created_archives[common_zip_name]["file_md5"],
            self.created_archives[common_zip_name]["file_md5_size"],
        ) = self.compute_file_md5(zip_path)

    def get_deps_files(self, path, child_path=None):
        if child_path is None:
            self.zip_files[path] = {}
        else:
            self.zip_files[path][child_path] = self.files[child_path]

        search_path = path if child_path is None else child_path
        if search_path in self.files:
            if "children" in self.files[search_path]:
                for child in self.files[search_path]["children"]:
                    if not self.files[child]["isInMainBundle"]:
                        if (
                            "nodeType" not in self.files[child]
                            or self.files[child]["nodeType"]
                            != "ExcludedCollectionProxy"
                        ):
                            self.get_deps_files(path, child)
                        else:
                            self.zip_files[path][child] = self.files[child]

    def create_debug_files(self):
        if self.debug_files:
            with open("unions.json", "w") as outfile:
                json.dump(self.common_files, outfile, indent=4)
            with open("sample.json", "w") as outfile:
                json.dump(self.zip_files, outfile, indent=4)

    def load_inputs(self):
        self.dmanifest = self.parse_protobuf_file(
            self.dmanifest_path, liveupdate_ddf_pb2.ManifestFile
        )
        if self.dmanifest is None:
            raise Exception("Error parsing dmanifest.")

        self.dmanifest_data = liveupdate_ddf_pb2.ManifestData()
        self.dmanifest_data.ParseFromString(self.dmanifest.data)

        self.index_resources()

        data = self.load_json_file(self.graph_path)
        if data is None:
            raise Exception("Error loading game.graph.json.")

        # Several paths may legitimately resolve to the same content (same
        # hexDigest) - e.g. an identical font shipped under two folders. We do
        # not abort on that: the bytes are stored once per archive (deduped by
        # hexDigest in add_files_to_zip) while each path keeps its manifest entry.
        duplicates_map = {}
        for element in data:
            hex_digest = element.get("hexDigest")
            if hex_digest is not None:
                if hex_digest in duplicates_map:
                    print(
                        f"Note: shared hexDigest {hex_digest} for "
                        f"{duplicates_map[hex_digest]} and {element['path']} "
                        "(stored once per archive, all manifest entries kept)"
                    )
                else:
                    duplicates_map[hex_digest] = element["path"]
                element["size"] = self.get_file_size(hex_digest)
            self.files[element["path"]] = element
            if (
                "nodeType" in element
                and element["nodeType"] == "ExcludedCollectionProxy"
            ):
                self.excluded_proxies[element["path"]] = element

    def index_resources(self):
        self.resources_by_url = {}
        self.resources_by_url_hash = {}
        self.resources_by_hash = {}
        for resource in self.dmanifest_data.resources:
            if resource.url:
                self.resources_by_url.setdefault(resource.url, []).append(resource)
            key = resource.url_hash & _MURMUR64_MASK
            self.resources_by_url_hash.setdefault(key, []).append(resource)
            self.resources_by_hash.setdefault(resource.hash.data.hex(), []).append(resource)

    def entries_for_path(self, path):
        # Some release url_hash values do not match hash64(graph path); the digest fallback covers them.
        for candidate in (path, "/" + path.lstrip("/"), path.lstrip("/")):
            found = self.resources_by_url.get(candidate)
            if found:
                return found
        for candidate in (path, "/" + path.lstrip("/"), path.lstrip("/")):
            found = self.resources_by_url_hash.get(path_url_hash(candidate))
            if found:
                return found
        info = self.files.get(path) or self.files.get("/" + path.lstrip("/"))
        digest = info.get("hexDigest") if info else None
        if digest:
            found = self.resources_by_hash.get(digest)
            if found:
                return found
        raise Exception(
            "No manifest entry for archive path "
            f"{path}. The archive manifest keeps only its own urls."
        )

    def entries_for_paths(self, paths):
        entries = []
        seen = set()
        for path in paths:
            for entry in self.entries_for_path(path):
                key = (entry.url, entry.url_hash)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(entry)
        return entries

    def manifest_entries_weight(self, paths):
        weight = 0
        for path in paths:
            for entry in self.entries_for_path(path):
                weight += len(entry.SerializeToString()) + 3
        return weight

    def dmanifest_fixed_weight(self):
        cached = getattr(self, "_dmanifest_fixed_weight", None)
        if cached is not None:
            return cached
        if self.dmanifest is None or self.dmanifest_data is None:
            return 0
        data = liveupdate_ddf_pb2.ManifestData()
        data.CopyFrom(self.dmanifest_data)
        data.ClearField("resources")
        manifest = liveupdate_ddf_pb2.ManifestFile()
        manifest.CopyFrom(self.dmanifest)
        manifest.data = data.SerializeToString()
        self._dmanifest_fixed_weight = len(manifest.SerializeToString())
        return self._dmanifest_fixed_weight

    def build_common_files(self):
        for path in self.zip_files:
            for res_name in self.zip_files[path]:
                if not res_name in self.common_files:
                    self.common_files[res_name] = {
                        "name": res_name,
                        "files": [],
                        "use_count": 0,
                    }
                file_exists = False
                for main_file in self.common_files[res_name]["files"]:
                    if main_file == self.files[path]["children"][0]:
                        file_exists = True
                        break
                if not file_exists:
                    self.common_files[res_name]["use_count"] += 1
                    self.common_files[res_name]["files"].append(
                        self.files[path]["children"][0]
                    )

    def precheck_files(self):
        all_file_paths = set()
        for archive_dict in (self.common_files,):
            for key in archive_dict.keys():
                all_file_paths.add(key)
        for path in self.zip_files:
            for key in self.zip_files[path]:
                all_file_paths.add(key)
        self.precheck_all_files_for_list(list(all_file_paths))

    def plan_zip_archive(self, name, files_list, common_files_list_name=None):
        # Different alias chunks can have the same content-derived name. Keep
        # every logical path so restore can resolve it at the other resolution.
        paths = self.archive_files.setdefault(name, [])
        known_paths = set(paths)
        for path in files_list:
            if path not in known_paths:
                paths.append(path)
                known_paths.add(path)
            if common_files_list_name:
                for collection in self.common_files.get(path, {}).get("files", []):
                    dependencies = self.dependency_list.setdefault(collection, [])
                    if name not in dependencies:
                        dependencies.append(name)

    def share_duplicate_content(self):
        archive_consumers = {}
        base_archives = set()
        for proxy in self.zip_files:
            collection = self.files[proxy]["children"][0]
            name = self.collection_name(collection)
            base_archives.add(name)
            archive_consumers.setdefault(name, set()).add(collection)
        for collection, dependencies in self.dependency_list.items():
            for archive in dependencies:
                archive_consumers.setdefault(archive, set()).add(collection)

        by_digest = {}
        for archive, paths in self.archive_files.items():
            for path in paths:
                digest = self.files[path]["hexDigest"]
                info = by_digest.setdefault(digest, {"archives": set()})
                info["archives"].add(archive)

        duplicate_digests = {digest for digest, info in by_digest.items()
                             if len(info["archives"]) > 1}
        shared_digests = set()
        groups = {}
        all_paths = list(dict.fromkeys(
            path for paths in self.archive_files.values() for path in paths
        ))
        for paths in self.resource_units(all_paths, self.archive_files.values()):
            digests = {self.files[path]["hexDigest"] for path in paths}
            if not digests.intersection(duplicate_digests):
                continue
            consumers = set()
            for digest in digests:
                for archive in by_digest[digest]["archives"]:
                    consumers.update(archive_consumers.get(archive, ()))
            if not consumers:
                raise ValueError(f"No consumers for shared resource hashes {sorted(digests)}")
            kinds = {
                "texture" if self.is_texture_resource(path) else
                "sound" if self.is_sound_resource(path) else "other"
                for path in paths
            }
            # Identical bytes across resource kinds still need a single owner.
            kind = next(iter(kinds)) if len(kinds) == 1 else "other"
            key = (tuple(sorted(consumers)), kind)
            groups.setdefault(key, set()).update(paths)
            shared_digests.update(digests)

        for archive, paths in list(self.archive_files.items()):
            paths = [path for path in paths
                     if self.files[path]["hexDigest"] not in shared_digests]
            if paths or archive in base_archives:
                self.archive_files[archive] = paths
            else:
                del self.archive_files[archive]
                for dependencies in self.dependency_list.values():
                    if archive in dependencies:
                        dependencies.remove(archive)

        for (consumers, kind), paths in sorted(groups.items()):
            prefix = {"other": "common", "texture": "common_texture",
                      "sound": "common_sound"}[kind]
            for chunk in self.split_by_size(sorted(paths)):
                name = f"{prefix}_{self.compute_version_hash_from_files(chunk)}"
                self.plan_zip_archive(name, chunk)
                for collection in consumers:
                    dependencies = self.dependency_list.setdefault(collection, [])
                    if name not in dependencies:
                        dependencies.append(name)

    def is_common_archive_name(self, name):
        return (
            name.startswith("common_")
            or name.startswith("common_texture_")
            or name.startswith("common_sound_")
        )

    def estimated_archive_weight(self, paths):
        if not paths:
            return self.dmanifest_fixed_weight()
        unique = {}
        for path in paths:
            info = self.files.get(path)
            if not info or not info.get("hexDigest"):
                continue
            unique[info["hexDigest"]] = info["size"]
        payload = sum(unique.values())
        try:
            entries = self.manifest_entries_weight(paths)
        except Exception:
            entries = 0
        return payload + self.dmanifest_fixed_weight() + entries

    def compression_mode_label(self):
        return "store" if self.zip_store else "deflate"

    def pack_name_for_members(self, member_names):
        hasher = hashlib.sha256()
        for name in sorted(member_names):
            hasher.update(name.encode("utf-8"))
            hasher.update(b"\0")
        hasher.update(b"compression:")
        hasher.update(self.compression_mode_label().encode("utf-8"))
        return PACK_PREFIX + self.truncate_hash(hasher.hexdigest())

    def build_merge_report_stats(self):
        by_kind = {
            "base": 0,
            "texture": 0,
            "sound": 0,
            "common": 0,
            "common_texture": 0,
            "common_sound": 0,
            "pack": 0,
            "other": 0,
        }
        sizes = []
        empty = 0
        for name, paths in self.archive_files.items():
            weight = self.estimated_archive_weight(paths)
            sizes.append(weight)
            if not paths:
                empty += 1
            if name.startswith(PACK_PREFIX):
                by_kind["pack"] += 1
            elif name.startswith("common_sound_"):
                by_kind["common_sound"] += 1
            elif name.startswith("common_texture_"):
                by_kind["common_texture"] += 1
            elif name.startswith("common_"):
                by_kind["common"] += 1
            elif name.endswith("_sound") or "_sound_" in name:
                by_kind["sound"] += 1
            elif name.endswith("_texture") or "_texture_" in name:
                by_kind["texture"] += 1
            else:
                by_kind["base"] += 1
        sizes.sort()
        small_16 = sum(1 for size in sizes if size < 16 * 1024)
        small_64 = sum(1 for size in sizes if size < 64 * 1024)
        return {
            "archive_count": len(self.archive_files),
            "empty_bases": empty,
            "by_kind": by_kind,
            "bytes_total_estimate": sum(sizes),
            "median_estimate": sizes[len(sizes) // 2] if sizes else 0,
            "small_under_16kib": small_16,
            "small_under_64kib": small_64,
            "merged_packs": len(self.merged_archives),
            "merged_members": sum(len(members) for members in self.merged_archives.values()),
        }

    def write_merge_report(self, report):
        artifact_dir = os.path.join("dist", "output")
        os.makedirs(artifact_dir, exist_ok=True)
        artifact = os.path.join(artifact_dir, "merge_report.json")
        with open(artifact, "w") as outfile:
            json.dump(report, outfile, indent=4)
        print(f"merge report: {artifact}")
        before = report.get("before_merge") or report
        print(
            "merge stats: "
            f"{before['archive_count']} archives, "
            f"{before['small_under_64kib']} < 64 KiB, "
            f"{before.get('merged_packs', 0)} packs "
            f"({before.get('merged_members', 0)} members)"
        )
        after = report.get("after_merge")
        if after:
            print(
                "after merge: "
                f"{after['archive_count']} archives, "
                f"{after['small_under_64kib']} < 64 KiB, "
                f"{after['merged_packs']} packs "
                f"({after['merged_members']} members)"
            )

    def module_order(self):
        import liveupdate_collections_report

        config = self.merge_config
        modules = liveupdate_collections_report.parse_lua_modules(config["modules_lua"])
        if modules is None:
            raise ValueError(f"Modules source not found: {config['modules_lua']}")
        order = {}
        labels = {}
        for index, module in enumerate(modules):
            labels.setdefault(module["label"], index)
            for file_name in module["files"]:
                order.setdefault(self.collection_stem(file_name), (index, module["label"]))
        patterns = []
        for rule in config.get("module_patterns", []):
            if rule["after"] not in labels:
                raise ValueError(f"Unknown module in module_patterns: {rule['after']}")
            patterns.append((rule["match"], labels[rule["after"]] + 0.5))
        last = len(modules) + 1

        def group_of(stem):
            if stem in order:
                return order[stem]
            for match, position in patterns:
                if fnmatch.fnmatch(stem + ".collectionc", match):
                    return (position, stem)
            return (last, stem)

        return group_of

    def collection_stem(self, path):
        return os.path.splitext(os.path.basename(path))[0]

    def archive_consumers(self):
        consumers = {}
        for proxy in self.zip_files:
            collection = self.files[proxy]["children"][0]
            consumers.setdefault(self.collection_name(collection), set()).add(self.collection_stem(collection))
        for collection, dependencies in self.dependency_list.items():
            for archive in dependencies:
                consumers.setdefault(archive, set()).add(self.collection_stem(collection))
        return consumers

    def is_media_archive_name(self, name, kind):
        return name.startswith(f"common_{kind}_") or name.endswith(f"_{kind}") or f"_{kind}_" in name

    def module_rules(self, label):
        config = self.merge_config
        rules = {key: config[key] for key in MERGE_RULE_KEYS}
        for pattern, override in config["modules"].items():
            if fnmatch.fnmatch(label, pattern):
                rules.update(override)
        return rules

    def is_excluded(self, name, stems):
        for pattern in self.merge_config["exclude"]:
            if fnmatch.fnmatch(name, pattern):
                return True
            if any(fnmatch.fnmatch(stem + ".collectionc", pattern) for stem in stems):
                return True
        return False

    def mergeable_archives(self, consumers, textures):
        for name, paths in sorted(self.archive_files.items()):
            if name.startswith(PACK_PREFIX) or not paths or name not in consumers:
                continue
            if self.is_media_archive_name(name, "sound"):
                continue
            if not textures and self.is_media_archive_name(name, "texture"):
                continue
            if self.is_excluded(name, consumers[name]):
                continue
            yield name, list(paths)

    def pack_bins(self, items, pack_limit, label):
        bins = []
        for weight, name, paths in sorted(items):
            for bucket in bins:
                combined = bucket["paths"] + paths
                new_weight = self.estimated_archive_weight(combined)
                if new_weight <= pack_limit:
                    bucket["members"].append(name)
                    bucket["paths"] = combined
                    bucket["weight"] = new_weight
                    break
            else:
                bins.append({"members": [name], "paths": list(paths), "weight": weight, "label": label})
        return bins

    def manual_pack_bins(self, consumers):
        bins = []
        claimed = set()
        for rule in self.merge_config["packs"]:
            collections = rule.get("collections", [])
            archives = rule.get("archives", [])
            max_archive = rule.get("max_archive_kib")
            items = []
            for name, paths in self.mergeable_archives(consumers, rule.get("textures", False)):
                if name in claimed:
                    continue
                stems = consumers[name]
                by_collection = bool(collections) and all(
                    any(fnmatch.fnmatch(stem + ".collectionc", pattern) for pattern in collections)
                    for stem in stems
                )
                by_name = any(fnmatch.fnmatch(name, pattern) for pattern in archives)
                if not by_collection and not by_name:
                    continue
                weight = self.estimated_archive_weight(paths)
                if max_archive is not None and weight >= max_archive * 1024:
                    continue
                items.append((weight, name, paths))
                claimed.add(name)
            pack_limit = min(rule.get("pack_limit_kib", self.merge_config["pack_limit_kib"]) * 1024, MERGE_SMALL_BUDGET)
            bins.extend(self.pack_bins(items, pack_limit, rule["name"]))
        return bins, claimed

    def merge_small_archives(self):
        group_of = self.module_order()
        consumers = self.archive_consumers()
        bins, claimed = self.manual_pack_bins(consumers)

        groups = {}
        for name, paths in self.mergeable_archives(consumers, True):
            if name in claimed:
                continue
            group = min(group_of(stem) for stem in consumers[name])
            rules = self.module_rules(group[1])
            if not rules["enabled"]:
                continue
            if not rules["textures"] and self.is_media_archive_name(name, "texture"):
                continue
            weight = self.estimated_archive_weight(paths)
            if weight >= rules["small_archive_kib"] * 1024:
                continue
            groups.setdefault(group, []).append((weight, name, paths))

        for group in sorted(groups):
            rules = self.module_rules(group[1])
            pack_limit = min(rules["pack_limit_kib"] * 1024, MERGE_SMALL_BUDGET)
            bins.extend(self.pack_bins(groups[group], pack_limit, group[1]))

        renamed = {}
        for bucket in bins:
            if len(bucket["members"]) < 2:
                continue
            pack_name = self.pack_name_for_members(bucket["members"])
            if pack_name in self.archive_files:
                raise Exception(f"Pack name collision: {pack_name}")
            for member in bucket["members"]:
                del self.archive_files[member]
                renamed[member] = pack_name
            self.archive_files[pack_name] = bucket["paths"]
            self.merged_archives[pack_name] = sorted(bucket["members"])
            print(
                f"Merged {len(bucket['members'])} archives -> {pack_name} "
                f"[{bucket['label']}] (~{bucket['weight'] / 1024:.1f} KiB est.)"
            )

        if not renamed:
            return

        for collection, dependencies in list(self.dependency_list.items()):
            rewritten = []
            seen = set()
            for dep in dependencies:
                replacement = renamed.get(dep, dep)
                if replacement not in seen:
                    rewritten.append(replacement)
                    seen.add(replacement)
            self.dependency_list[collection] = rewritten

    def write_planned_archives(self, share_duplicates=True, allow_merge=True):
        # Low follows the high layout: a low-only common_* can shadow a mounted high.
        if share_duplicates:
            self.share_duplicate_content()
        self.verify_one_path_one_archive()
        report = {"before_merge": self.build_merge_report_stats(), "merge_config": self.merge_config}
        if self.merge_config and allow_merge:
            self.merge_small_archives()
            self.verify_one_path_one_archive()
            report["after_merge"] = self.build_merge_report_stats()
            report["merged_archives"] = dict(self.merged_archives)
        elif self.merged_archives:
            report["after_merge"] = self.build_merge_report_stats()
            report["merged_archives"] = dict(self.merged_archives)
        self.write_merge_report(report)
        for name, paths in self.archive_files.items():
            self.create_zip_archive(name, paths)

    def restore_from_files_tree(self):
        print("Restoring from original files tree")
        original_files_tree = self.load_json_file("files_tree.json")
        manifest_output = original_files_tree["manifest"]
        high_versions = dict(manifest_output.get("file_versions", {}))
        high_deps = {
            archive: list(names)
            for archive, names in manifest_output.get("deps", {}).items()
        }
        self.merged_archives = {
            pack: list(members)
            for pack, members in (manifest_output.get("merged_archives") or {}).items()
        }
        collections = {
            self.collection_name(self.files[proxy]["children"][0]):
            self.files[proxy]["children"][0]
            for proxy in self.zip_files
        }
        self.dependency_list = {}
        for archive, names in high_deps.items():
            for name in names:
                collection = collections.get(name)
                if collection is None:
                    raise Exception(
                        "High manifest depends on "
                        f"{name}, which is not an excluded collection in this graph"
                    )
                self.dependency_list.setdefault(collection, []).append(archive)
        for name, info in original_files_tree["zip_files"].items():
            self.plan_zip_archive(name, [file["path"] for file in info["files"]])

        self.verify_restore_matches_tree(original_files_tree)
        self.write_planned_archives(share_duplicates=False, allow_merge=False)
        self.rename_archives_to_version()
        restored_manifest = self.build_manifest_output()
        self.verify_manifest_pair(
            {"file_versions": high_versions, "deps": high_deps},
            restored_manifest,
        )
        self.report_base_divergence(original_files_tree, high_versions, restored_manifest)
        for key in (
            "version",
            "deps",
            "file_versions",
            "file_sizes",
            "file_md5",
            "file_md5_size",
            "md5_chunk_size",
            "merged_archives",
            "dmanifest_info",
        ):
            if key in restored_manifest:
                manifest_output[key] = restored_manifest[key]
        if self.client_version is not None:
            manifest_output["client_version"] = self.client_version
        self.merged_archives = dict(restored_manifest.get("merged_archives") or {})
        self.write_outputs(manifest_output)
        os.remove("files_tree.json")

    def packable_paths(self):
        paths = []
        seen = set()
        for grouped in self.zip_files.values():
            for path, info in grouped.items():
                if path in seen or not info or not info.get("hexDigest"):
                    continue
                if info.get("isInMainBundle"):
                    continue
                seen.add(path)
                paths.append(path)
        return paths

    def verify_one_path_one_archive(self):
        owners = {}
        for name, paths in self.archive_files.items():
            for path in paths:
                if path in owners:
                    raise Exception(
                        f"{path} is packed into both {owners[path]} and {name}"
                    )
                owners[path] = name
        missing = [
            path for path in self.packable_paths() if path not in owners
        ]
        if missing:
            preview = ", ".join(missing[:20])
            raise Exception(
                f"{len(missing)} excluded path(s) are in no archive: {preview}"
            )

    def verify_restore_matches_tree(self, original_files_tree):
        tree = original_files_tree["zip_files"]
        planned_names = set(self.archive_files)
        tree_names = set(tree)
        if planned_names != tree_names:
            raise Exception(
                "Restored archive names differ from the high files tree: "
                f"only in high {sorted(tree_names - planned_names)}, "
                f"only in this resolution {sorted(planned_names - tree_names)}"
            )
        absent = []
        for name, info in tree.items():
            tree_paths = [item["path"] for item in info["files"]]
            planned = self.archive_files[name]
            if sorted(tree_paths) != sorted(planned):
                raise Exception(f"Archive {name} paths differ from the high files tree")
            for path in tree_paths:
                if path not in self.files:
                    absent.append(path)
                    continue
                self.entries_for_path(path)
        if absent:
            preview = ", ".join(absent[:20])
            raise Exception(
                f"{len(absent)} high-tree path(s) are missing from this graph: {preview}"
            )

    def verify_manifest_pair(self, high_manifest, low_manifest):
        high_names = set(high_manifest.get("file_versions", {}))
        low_names = set(low_manifest.get("file_versions", {}))
        if high_names != low_names:
            raise Exception(
                "Archive name sets differ between resolutions: "
                f"only in high {sorted(high_names - low_names)}, "
                f"only in low {sorted(low_names - high_names)}"
            )

        def normalize(deps):
            return {name: sorted(set(consumers)) for name, consumers in deps.items()}

        high_deps = normalize(high_manifest.get("deps", {}))
        low_deps = normalize(low_manifest.get("deps", {}))
        if high_deps != low_deps:
            raise Exception(
                "Archive dependencies differ between resolutions: "
                f"high {high_deps}, low {low_deps}"
            )

    def is_base_archive(self, name):
        if name.startswith("common_texture_") or name.startswith("common_sound_"):
            return False
        if name.startswith("common_"):
            return True
        for kind in ("texture", "sound"):
            if f"{COLLECTION_SUFFIX}_{kind}" in name:
                return False
        return True

    def report_base_divergence(self, original_files_tree, high_versions, low_manifest):
        diverged = []
        for name, low_version in sorted(low_manifest.get("file_versions", {}).items()):
            if not self.is_base_archive(name):
                continue
            high_version = high_versions.get(name)
            if high_version == low_version:
                continue
            causes = []
            unchanged = []
            for item in original_files_tree["zip_files"].get(name, {}).get("files", []):
                path = item["path"]
                low_info = self.files.get(path) or {}
                if item.get("hexDigest") != low_info.get("hexDigest"):
                    causes.append(path)
                else:
                    unchanged.append(path)
            diverged.append((name, high_version, low_version, causes, unchanged))

        if not diverged:
            print("Base archives: low and high versions match.")
            return diverged

        print(f"Base archives whose version differs between low and high: {len(diverged)}")
        for name, high_version, low_version, causes, unchanged in diverged:
            print(f"  {name}: high {high_version} low {low_version}")
            if causes:
                for path in causes:
                    print(f"    {path}")
            else:
                print("    content hashes match; manifest entries differ")
                for path in unchanged:
                    print(f"    {path}")
        return diverged

    def check_proxy_root_basenames(self):
        seen = {}
        for proxy, element in self.excluded_proxies.items():
            children = element.get("children") or []
            if not children:
                raise Exception(f"Excluded collection proxy has no root collection: {proxy}")
            root = children[0]
            name = self.collection_name(root)
            if name in seen:
                raise Exception(
                    "Excluded collection proxies share archive name "
                    f"{name}: {seen[name]} and {root}"
                )
            seen[name] = root

    def resource_units(self, file_keys, related_groups=None):
        # Aliases, texture pairs and sound/audio edges form indivisible units.
        # Components also handle transitive links (e.g. audio with two parents).
        parents = {key: key for key in file_keys}

        def find(key):
            while parents[key] != key:
                parents[key] = parents[parents[key]]
                key = parents[key]
            return key

        def join(first, second):
            parents[find(second)] = find(first)

        digest_paths = {}
        for key in file_keys:
            digest = self.files[key]["hexDigest"]
            if digest in digest_paths:
                join(key, digest_paths[digest])
            else:
                digest_paths[digest] = key

        # During promotion preserve pairs already colocated in a planned archive;
        # do not join media deliberately routed to separate dependency groups.
        for group in (related_groups if related_groups is not None else [file_keys]):
            keys = set(group)
            for key in group:
                if key.endswith(".texturec"):
                    pair = key[:-len(".texturec")] + ".a.texturesetc"
                    if pair in keys:
                        join(key, pair)
                if key.endswith(".soundc"):
                    for child in self.files[key].get("children", []):
                        if self.is_sound_child(child) and child in keys:
                            join(key, child)

        units = {}
        for key in file_keys:
            units.setdefault(find(key), []).append(key)
        return list(units.values())

    def split_by_size(self, file_keys):
        if MAX_ARCHIVE_SIZE <= 0:
            return [list(file_keys)]

        chunks, chunk, payload, manifest_weight = [], [], 0, self.dmanifest_fixed_weight()
        for keys in self.resource_units(file_keys):
            unique_files = {self.files[key]["hexDigest"]: self.files[key] for key in keys}
            unit_payload = sum(info["size"] for info in unique_files.values())
            unit_manifest = self.manifest_entries_weight(keys)
            if chunk and payload + unit_payload + manifest_weight + unit_manifest > MAX_ARCHIVE_SIZE:
                chunks.append(chunk)
                chunk, payload, manifest_weight = [], 0, self.dmanifest_fixed_weight()
            chunk.extend(keys)
            payload += unit_payload
            manifest_weight += unit_manifest
        if chunk:
            chunks.append(chunk)
        return chunks

    def is_texture_resource(self, resource_path):
        return resource_path.endswith(".texturec") or resource_path.endswith(
            ".a.texturesetc"
        )

    def is_sound_resource(self, resource_path):
        # A sound component (.soundc) plus its compiled audio children
        # (.oggc / .opusc / .wavc). Unlike a texture pair, a .soundc and its
        # audio do NOT share a stem — the link is a graph "children" edge and is
        # 1:N — so co-location is done via self.files[...]["children"] in
        # split_by_size, not by name.
        return resource_path.endswith((".soundc", ".oggc", ".opusc", ".wavc"))

    def is_sound_child(self, resource_path):
        return resource_path.endswith((".oggc", ".opusc", ".wavc"))

    def truncate_hash(self, hex_digest):
        return hex_digest[:HASH_LEN]

    def collection_name(self, resource_path):
        # "<id>.collectionc" -> "<id>" + COLLECTION_SUFFIX. With the default
        # suffix this reproduces os.path.basename() byte-for-byte.
        stem = os.path.splitext(os.path.basename(resource_path))[0]
        return stem + COLLECTION_SUFFIX

    def compute_version_hash_from_files(self, files_list):
        for filepath in files_list:
            file_info = self.files.get(filepath)
            if not file_info:
                raise Exception(f"File info not found for {filepath}")
            if not file_info.get("hexDigest"):
                raise Exception(f"Missing hexDigest for file: {filepath}")
        version_hasher = hashlib.sha256()
        for resource_entry in sorted(
            self.entries_for_paths(files_list),
            key=lambda item: (item.hash.data.hex(), item.url),
        ):
            version_hasher.update(resource_entry.SerializeToString())
        return self.truncate_hash(version_hasher.hexdigest())

    def create_common_archives_by_dependency_sets(self):
        groups = {}
        for res_name, info in self.common_files.items():
            if info["use_count"] <= 1:
                continue
            key = tuple(sorted(info["files"]))
            if key not in groups:
                groups[key] = {"textures": [], "sounds": [], "others": []}
            if self.is_texture_resource(res_name):
                groups[key]["textures"].append(res_name)
            elif self.is_sound_resource(res_name):
                groups[key]["sounds"].append(res_name)
            else:
                groups[key]["others"].append(res_name)

        for key in sorted(groups.keys()):
            texture_files = sorted(groups[key]["textures"])
            sound_files = sorted(groups[key]["sounds"])
            other_files = sorted(groups[key]["others"])

            if other_files:
                for chunk in self.split_by_size(other_files):
                    version_hash = self.compute_version_hash_from_files(chunk)
                    archive_name = f"common_{version_hash}"
                    self.plan_zip_archive(archive_name, chunk, archive_name)

            if texture_files:
                for chunk in self.split_by_size(texture_files):
                    version_hash = self.compute_version_hash_from_files(chunk)
                    archive_name = f"common_texture_{version_hash}"
                    self.plan_zip_archive(archive_name, chunk, archive_name)

            if sound_files:
                for chunk in self.split_by_size(sound_files):
                    version_hash = self.compute_version_hash_from_files(chunk)
                    archive_name = f"common_sound_{version_hash}"
                    self.plan_zip_archive(archive_name, chunk, archive_name)

    def is_shared_resource(self, resource_path):
        # Resources used by more than one collection are packed into common_*
        # archives (see create_common_archives_by_dependency_sets). Packing them
        # per-collection as well would duplicate payloads unnecessarily: each
        # collection already depends on the common archive that holds them.
        return self.common_files.get(resource_path, {}).get("use_count", 0) > 1

    def create_collection_archives(self):
        print("Starting zip file collection")
        for path in self.zip_files:
            zip_file_name = self.collection_name(self.files[path]["children"][0])
            files_list = [f for f in self.zip_files[path].keys() if not self.is_shared_resource(f)]
            texture_files = [f for f in files_list if self.is_texture_resource(f)]
            sound_files = [f for f in files_list if self.is_sound_resource(f)]
            other_files = [
                f
                for f in files_list
                if not self.is_texture_resource(f) and not self.is_sound_resource(f)
            ]

            # Always emit the base collection archive (<name> + COLLECTION_SUFFIX):
            # the liveupdater requires every collection module's base name to exist in the
            # manifest (is_module_loaded / check_modules_integrity). A collection's
            # own data may move to common archives when it is byte-identical to
            # another collection. Keep its base archive even when it is empty.
            self.plan_zip_archive(zip_file_name, other_files)

            if texture_files:
                main_file = self.files[path]["children"][0]
                if main_file not in self.dependency_list:
                    self.dependency_list[main_file] = []
                base_name = f"{zip_file_name}_texture"
                texture_chunks = self.split_by_size(texture_files)
                for chunk in texture_chunks:
                    if len(texture_chunks) == 1:
                        texture_archive_name = base_name
                    else:
                        version_hash = self.compute_version_hash_from_files(chunk)
                        texture_archive_name = f"{base_name}_{version_hash}"
                    self.plan_zip_archive(texture_archive_name, chunk)
                    if texture_archive_name not in self.dependency_list[main_file]:
                        self.dependency_list[main_file].append(texture_archive_name)

            if sound_files:
                main_file = self.files[path]["children"][0]
                if main_file not in self.dependency_list:
                    self.dependency_list[main_file] = []
                base_name = f"{zip_file_name}_sound"
                sound_chunks = self.split_by_size(sound_files)
                for chunk in sound_chunks:
                    if len(sound_chunks) == 1:
                        sound_archive_name = base_name
                    else:
                        version_hash = self.compute_version_hash_from_files(chunk)
                        sound_archive_name = f"{base_name}_{version_hash}"
                    self.plan_zip_archive(sound_archive_name, chunk)
                    if sound_archive_name not in self.dependency_list[main_file]:
                        self.dependency_list[main_file].append(sound_archive_name)
        print("Zip file collection completed.")

    def build_manifest_output(self):
        manifest_output = {
            "version": self.current_timestamp,
            "deps": {},
            "file_versions": self.build_file_versions(),
            "file_sizes": self.build_file_sizes(),
            "file_md5": self.build_file_md5(),
            "file_md5_size": self.build_file_md5_size(),
            "md5_chunk_size": MD5_CHUNK_SIZE,
            "dmanifest_info": self.build_dmanifest_info(),
        }
        if self.merged_archives:
            manifest_output["merged_archives"] = {
                pack: list(members) for pack, members in sorted(self.merged_archives.items())
            }
        if self.client_version is not None:
            manifest_output["client_version"] = self.client_version

        for filepath, archives in self.dependency_list.items():
            for archive in archives:
                if archive not in manifest_output["deps"]:
                    manifest_output["deps"][archive] = []

                file_name = self.collection_name(filepath)
                manifest_output["deps"][archive].append(file_name)

        return manifest_output

    def write_outputs(self, manifest_output):
        with open(os.path.join(self.result_folder, "manifest.json"), "w") as outfile:
            json.dump(manifest_output, outfile, indent=4)

        self.write_files_tree(manifest_output)
        self.create_debug_files()

    def write_files_tree(self, manifest_output):
        # Not in liveupdate_zip/: that folder is published to the CDN.
        self.files_tree["manifest"] = manifest_output
        with open("files_tree.json", "w") as outfile:
            json.dump(self.files_tree, outfile, indent=4)
        if self.restore_from_tree:
            return
        artifact_dir = os.path.join("dist", "output")
        os.makedirs(artifact_dir, exist_ok=True)
        artifact = os.path.join(artifact_dir, "files_tree.json")
        with open(artifact, "w") as outfile:
            json.dump(self.files_tree, outfile, indent=4)
        print(f"files_tree artifact: {artifact}")

    def compute_file_md5(self, file_path):
        size = self.md5_covered_size(file_path)
        parts = []
        with open(file_path, "rb") as file_obj:
            left = size
            while left > 0:
                chunk = file_obj.read(min(MD5_CHUNK_SIZE, left))
                if not chunk:
                    break
                parts.append(hashlib.md5(chunk).hexdigest())
                left -= len(chunk)
        return hashlib.md5("".join(parts).encode()).hexdigest(), size

    def md5_covered_size(self, file_path):
        with zipfile.ZipFile(file_path) as bundle:
            entries = bundle.infolist()
        manifests = [entry for entry in entries if entry.filename == self.dmanifest_name]
        if not manifests:
            return os.path.getsize(file_path)
        offset = manifests[0].header_offset
        if any(entry.header_offset > offset for entry in entries):
            raise ValueError(f"{file_path}: {self.dmanifest_name} must be the last zip entry")
        return offset

    def rename_archives_to_version(self):
        for common_name, archive_info in self.created_archives.items():
            final_name = (
                common_name
                + "_"
                + archive_info["version_hash"]
                + self.extension_archive
            )
            final_path = os.path.join(self.result_folder, final_name)
            if archive_info["path"] != final_path:
                os.replace(archive_info["path"], final_path)
                archive_info["path"] = final_path
            print(f"Final archive: {final_name}")

    def build_file_versions(self):
        file_versions = {}
        for common_name, archive_info in self.created_archives.items():
            file_versions[common_name] = archive_info["version_hash"]
        return file_versions

    # sizes live in a separate key so that clients deployed before this field
    # existed keep parsing file_versions as plain strings
    def build_file_sizes(self):
        file_sizes = {}
        for common_name, archive_info in self.created_archives.items():
            file_sizes[common_name] = archive_info["size"]
        return file_sizes

    def build_file_md5(self):
        file_md5 = {}
        for common_name, archive_info in self.created_archives.items():
            file_md5[common_name] = archive_info["file_md5"]
        return file_md5

    def build_file_md5_size(self):
        file_md5_size = {}
        for common_name, archive_info in self.created_archives.items():
            file_md5_size[common_name] = archive_info["file_md5_size"]
        return file_md5_size

    def build_dmanifest_info(self):
        header = self.dmanifest_data.header

        def hash_digest_to_hex(hash_digest):
            return hash_digest.data.hex()

        hash_algo_map = {
            0: "HASH_UNKNOWN",
            1: "HASH_MD5",
            2: "HASH_SHA1",
            3: "HASH_SHA256",
            4: "HASH_SHA512",
        }

        sign_algo_map = {
            0: "SIGN_UNKNOWN",
            1: "SIGN_RSA",
        }

        return {
            "signature": self.dmanifest.signature.hex(),
            "archive_identifier": self.dmanifest.archive_identifier.hex(),
            "version": self.dmanifest.version,
            "header": {
                "resource_hash_algorithm": hash_algo_map.get(
                    header.resource_hash_algorithm, str(header.resource_hash_algorithm)
                ),
                "signature_hash_algorithm": hash_algo_map.get(
                    header.signature_hash_algorithm,
                    str(header.signature_hash_algorithm),
                ),
                "signature_sign_algorithm": sign_algo_map.get(
                    header.signature_sign_algorithm,
                    str(header.signature_sign_algorithm),
                ),
                "project_identifier": hash_digest_to_hex(header.project_identifier),
            },
            "engine_versions": [
                hash_digest_to_hex(item) for item in self.dmanifest_data.engine_versions
            ],
        }

    def run(self):
        self.load_inputs()

        for proxy_path in self.excluded_proxies:
            self.get_deps_files(proxy_path)
        self.check_proxy_root_basenames()

        if not os.path.exists(self.result_folder):
            os.makedirs(self.result_folder)

        self.build_common_files()
        self.precheck_files()

        if self.restore_from_tree:
            self.restore_from_files_tree()
            sys.exit(0)

        self.create_common_archives_by_dependency_sets()
        self.create_collection_archives()
        self.write_planned_archives()

        self.rename_archives_to_version()

        manifest_output = self.build_manifest_output()
        self.write_outputs(manifest_output)

        print("Process completed successfully.")


def load_merge_config(data):
    merge = data.get("merge")
    if not merge or not merge.get("enabled", True):
        return None
    check_keys("merge", merge, MERGE_KEYS)
    if not merge.get("modules_lua"):
        raise ValueError("merge.modules_lua: need the client modules file")
    config = {
        "modules_lua": merge["modules_lua"],
        "module_patterns": list(merge.get("module_patterns", [])),
        "enabled": True,
        "small_archive_kib": int(merge.get("small_archive_kib", 64)),
        "pack_limit_kib": int(merge.get("pack_limit_kib", 4096)),
        "textures": bool(merge.get("textures", False)),
        "modules": {},
        "packs": [],
        "exclude": [str(pattern) for pattern in merge.get("exclude", [])],
    }
    check_limits("merge", config)
    for rule in config["module_patterns"]:
        check_keys("merge.module_patterns", rule, ("match", "after"))
    for pattern, override in merge.get("modules", {}).items():
        check_keys(f"merge.modules.{pattern}", override, MERGE_RULE_KEYS)
        rules = {key: config[key] for key in MERGE_RULE_KEYS}
        rules.update(override)
        check_limits(f"merge.modules.{pattern}", rules)
        config["modules"][pattern] = dict(override)
    names = set()
    for rule in merge.get("packs", []):
        check_keys("merge.packs", rule, ("name", "collections", "archives", "max_archive_kib", "pack_limit_kib", "textures"))
        name = rule.get("name")
        if not name or name in names:
            raise ValueError(f"merge.packs: need a unique name, got {name!r}")
        if not rule.get("collections") and not rule.get("archives"):
            raise ValueError(f"merge.packs.{name}: need collections or archives")
        names.add(name)
        config["packs"].append(dict(rule))
    return config


def read_merge_config(path):
    if not os.path.isfile(path):
        print(f"merge: {path} not found")
        return None
    with open(path) as config_file:
        text = config_file.read()
    if not text.strip():
        return None
    return load_merge_config(json.loads(text))


def check_keys(where, section, allowed):
    unknown = sorted(set(section) - set(allowed))
    if unknown:
        raise ValueError(f"{where}: unknown keys {unknown}")


def check_limits(where, rules):
    if rules["small_archive_kib"] <= 0 or rules["pack_limit_kib"] <= rules["small_archive_kib"]:
        raise ValueError(f"{where}: need 0 < small_archive_kib < pack_limit_kib")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore_from_tree", action="store_true")
    parser.add_argument("--client-version")
    parser.add_argument(
        "--zip-store",
        action="store_true",
        help="store archives up to 24 MiB, deflate after that, reject a file over 25 MiB",
    )
    parser.add_argument(
        "--merge-config",
        help="JSON with a \"merge\" section: small archives into pack_* per client module; writes merge_report.json",
    )
    options = parser.parse_args()
    if options.client_version is not None and not options.client_version.strip():
        parser.error("--client-version must be a non-empty string")
    merge_config = None
    if options.merge_config:
        merge_config = read_merge_config(options.merge_config)
    PackContext(
        options.restore_from_tree,
        options.client_version,
        options.zip_store,
        merge_config,
    ).run()


if __name__ == "__main__":
    main()
