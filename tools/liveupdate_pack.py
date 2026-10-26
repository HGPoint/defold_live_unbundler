import argparse
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


class PackContext:
    def __init__(self, restore_from_tree=False, client_version=None):
        self.restore_from_tree = restore_from_tree
        self.client_version = client_version
        print("restore_from_tree: ", self.restore_from_tree)

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

        self.dmanifest = None
        self.dmanifest_data = None
        self.files = {}
        self.excluded_proxies = {}
        self.zip_files = {}
        self.common_files = {}
        self.manifest_data_resources = {}
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

    def add_files_to_zip(
        self,
        common_zip_name,
        zip_file,
        files_list,
        dmanifest_data,
        zip_name,
        manifest_data_resources,
        content_hashers,
        resources_list,
        common_files_list_name=None,
    ):
        for filepath in files_list:
            file_info = self.files.get(filepath)
            if not file_info:
                print(f"Warning: File info not found for {filepath}")
                continue
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
                    zinfo.compress_type = zipfile.ZIP_DEFLATED
                    zip_file.writestr(zinfo, file_contents)

                    self.added_files[self.files[filepath]["hexDigest"]] = zip_name

                    for resource_entry in manifest_data_resources[
                        self.files[filepath]["hexDigest"]
                    ]:
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
                self.manifest_data_resources,
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
            dmanifest_info.compress_type = zipfile.ZIP_DEFLATED
            zip_file.writestr(dmanifest_info, dmanifest_bytes)
        content_hash_no_manifest = content_hashers["no_manifest"].hexdigest()
        version_hasher = hashlib.sha256()
        for resource_entry in sorted(
            resources_list, key=lambda item: item.hash.data.hex()
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

        # One content hash may map to several ResourceEntry records (the same
        # bytes referenced by different urls/url_hash). Keep them all so every
        # url variant survives into the generated archive manifest.
        self.manifest_data_resources = {}
        for resource in self.dmanifest_data.resources:
            self.manifest_data_resources.setdefault(
                resource.hash.data.hex(), []
            ).append(resource)

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

    def write_planned_archives(self):
        self.share_duplicate_content()
        for name, paths in self.archive_files.items():
            self.create_zip_archive(name, paths)

    def restore_from_files_tree(self):
        print("Restoring from original files tree")
        original_files_tree = self.load_json_file("files_tree.json")
        manifest_output = original_files_tree["manifest"]
        collections = {
            self.collection_name(self.files[proxy]["children"][0]):
            self.files[proxy]["children"][0]
            for proxy in self.zip_files
        }
        self.dependency_list = {}
        for archive, names in manifest_output["deps"].items():
            for name in names:
                self.dependency_list.setdefault(collections[name], []).append(archive)
        for name, info in original_files_tree["zip_files"].items():
            self.plan_zip_archive(name, [file["path"] for file in info["files"]])

        # A different resolution may merge previously distinct content hashes.
        # Reconcile owners/dependencies before replaying the saved layout.
        self.write_planned_archives()
        self.rename_archives_to_version()
        restored_manifest = self.build_manifest_output()
        for key in ("version", "deps", "file_versions", "file_sizes", "dmanifest_info"):
            manifest_output[key] = restored_manifest[key]
        if self.client_version is not None:
            manifest_output["client_version"] = self.client_version
        self.write_outputs(manifest_output)
        os.remove("files_tree.json")

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

        chunks, chunk, size = [], [], 0
        for keys in self.resource_units(file_keys):
            unique_files = {self.files[key]["hexDigest"]: self.files[key] for key in keys}
            unit_size = sum(info["size"] for info in unique_files.values())
            if chunk and size + unit_size > MAX_ARCHIVE_SIZE:
                chunks.append(chunk)
                chunk, size = [], 0
            chunk.extend(keys)
            size += unit_size
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
        version_hasher = hashlib.sha256()
        resources = []
        seen_digests = set()
        for filepath in files_list:
            file_info = self.files.get(filepath)
            if not file_info:
                continue
            hex_digest = file_info.get("hexDigest")
            if not hex_digest:
                raise Exception(f"Missing hexDigest for file: {filepath}")
            if hex_digest not in seen_digests:
                resources.extend(self.manifest_data_resources[hex_digest])
                seen_digests.add(hex_digest)
        for resource_entry in sorted(resources, key=lambda item: item.hash.data.hex()):
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
            "dmanifest_info": self.build_dmanifest_info(),
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

        self.files_tree["manifest"] = manifest_output
        with open("files_tree.json", "w") as outfile:
            json.dump(self.files_tree, outfile, indent=4)

        with open(os.path.join(self.result_folder, "files_tree.json"), "w") as outfile:
            json.dump(self.files_tree, outfile, indent=4)

        self.create_debug_files()

    def compute_file_hash(self, file_path):
        hasher = hashlib.sha256()
        with open(file_path, "rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore_from_tree", action="store_true")
    parser.add_argument("--client-version")
    options = parser.parse_args()
    if options.client_version is not None and not options.client_version.strip():
        parser.error("--client-version must be a non-empty string")
    PackContext(options.restore_from_tree, options.client_version).run()


if __name__ == "__main__":
    main()
