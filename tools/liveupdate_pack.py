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


class PackContext:
    def __init__(self):
        self.restore_from_tree = (
            len(sys.argv) > 1 and sys.argv[1] == "--restore_from_tree"
        )
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
            if not self.restore_from_tree:
                if common_zip_name not in self.files_tree["zip_files"]:
                    self.files_tree["zip_files"][common_zip_name] = {
                        "files": [],
                        "size": 0,
                    }
                self.files_tree["zip_files"][common_zip_name]["files"].append(
                    self.files[filepath]
                )
                self.files_tree["zip_files"][common_zip_name]["size"] += self.files[
                    filepath
                ]["size"]
            if self.files[filepath]["hexDigest"] not in self.added_files:
                with open(file_path_hex, "rb") as file_obj:
                    file_contents = file_obj.read()
                    content_hashers["all"].update(file_contents)
                    content_hashers["no_manifest"].update(file_contents)
                    zip_file.write(
                        file_path_hex, arcname=self.files[filepath]["hexDigest"]
                    )

                    self.added_files[self.files[filepath]["hexDigest"]] = zip_name

                    resource_entry = manifest_data_resources[
                        self.files[filepath]["hexDigest"]
                    ]
                    dmanifest_data.resources.append(resource_entry)
                    resources_list.append(resource_entry)

            if common_files_list_name:
                for main_file in self.common_files.get(filepath, {}).get("files", []):
                    if main_file not in self.dependency_list:
                        self.dependency_list[main_file] = []
                    if common_files_list_name not in self.dependency_list[main_file]:
                        self.dependency_list[main_file].append(common_files_list_name)

    def create_zip_archive(self, zip_name, files_list, common_files_list_name=None):
        common_zip_name = zip_name
        zip_name = zip_name + self.temp_suffix
        zip_path = os.path.join(self.result_folder, zip_name + self.extension_archive)
        self.dmanifest_data.ClearField("resources")
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
            zip_file.writestr(self.dmanifest_name, dmanifest_bytes)
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

        self.manifest_data_resources = {
            resource.hash.data.hex(): resource
            for resource in self.dmanifest_data.resources
        }

        data = self.load_json_file(self.graph_path)
        if data is None:
            raise Exception("Error loading game.graph.json.")

        duplicates_map = {}
        for element in data:
            if element["hexDigest"] in duplicates_map:
                raise Exception(
                    f"Duplicate hexDigest found: {element}, duplicates_map: {duplicates_map[element['hexDigest']]}"
                )
            else:
                duplicates_map[element["hexDigest"]] = element
            if "hexDigest" in element and element["hexDigest"] is not None:
                element["size"] = self.get_file_size(element["hexDigest"])
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

    def restore_from_files_tree(self):
        print("Restoring from original files tree")
        original_files_tree = self.load_json_file("files_tree.json")
        manifest_output = original_files_tree["manifest"]
        for zip_file_name in original_files_tree["zip_files"]:
            restored_zip_files = []
            for file in original_files_tree["zip_files"][zip_file_name]["files"]:
                restored_zip_files.append(file["path"])

            self.create_zip_archive(zip_file_name, restored_zip_files, zip_file_name)

        self.rename_archives_to_version()

        manifest_output["version"] = self.current_timestamp
        for common_name, archive_info in self.created_archives.items():
            entry = manifest_output["files"].get(common_name)
            if entry is None:
                manifest_output["files"][common_name] = [archive_info["version_hash"]]
            else:
                entry[0] = archive_info["version_hash"]
        manifest_output["dmanifest_info"] = self.build_dmanifest_info()
        with open(os.path.join(self.result_folder, "manifest.json"), "w") as outfile:
            json.dump(manifest_output, outfile, indent=4)
        os.remove("files_tree.json")

    def split_by_size(self, file_keys):
        if MAX_ARCHIVE_SIZE <= 0:
            return [list(file_keys)]
        file_sized_lists = []
        fs_list = []
        size = 0

        key_index = {key: index for index, key in enumerate(file_keys)}
        key_set = set(file_keys)
        used = set()

        def make_unit(keys):
            unit_size = 0
            for k in keys:
                unit_size += self.files[k]["size"]
            return {"keys": keys, "size": unit_size}

        units = []
        for key in file_keys:
            if key in used:
                continue
            used.add(key)
            if key.endswith(".texturec"):
                base = key[: -len(".texturec")]
                pair = base + ".a.texturesetc"
                if pair in key_set and pair not in used:
                    used.add(pair)
                    pair_order = (
                        [key, pair] if key_index[key] < key_index[pair] else [pair, key]
                    )
                    units.append(make_unit(pair_order))
                    continue
            elif key.endswith(".a.texturesetc"):
                base = key[: -len(".a.texturesetc")]
                pair = base + ".texturec"
                if pair in key_set and pair not in used:
                    used.add(pair)
                    pair_order = (
                        [key, pair] if key_index[key] < key_index[pair] else [pair, key]
                    )
                    units.append(make_unit(pair_order))
                    continue
            units.append(make_unit([key]))

        for unit in units:
            if size + unit["size"] > MAX_ARCHIVE_SIZE and fs_list:
                file_sized_lists.append(fs_list)
                fs_list = []
                size = 0
            if unit["size"] > MAX_ARCHIVE_SIZE and not fs_list:
                file_sized_lists.append(unit["keys"])
                continue
            size += unit["size"]
            fs_list.extend(unit["keys"])

        if fs_list:
            file_sized_lists.append(fs_list)
        return file_sized_lists

    def is_texture_resource(self, resource_path):
        return resource_path.endswith(".texturec") or resource_path.endswith(
            ".a.texturesetc"
        )

    def truncate_hash(self, hex_digest):
        return hex_digest[:HASH_LEN]

    def compute_version_hash_from_files(self, files_list):
        version_hasher = hashlib.sha256()
        resources = []
        for filepath in files_list:
            file_info = self.files.get(filepath)
            if not file_info:
                continue
            hex_digest = file_info.get("hexDigest")
            if not hex_digest:
                raise Exception(f"Missing hexDigest for file: {filepath}")
            resource_entry = self.manifest_data_resources[hex_digest]
            resources.append(resource_entry)
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
                groups[key] = {"textures": [], "others": []}
            if self.is_texture_resource(res_name):
                groups[key]["textures"].append(res_name)
            else:
                groups[key]["others"].append(res_name)

        for key in sorted(groups.keys()):
            texture_files = sorted(groups[key]["textures"])
            other_files = sorted(groups[key]["others"])

            if other_files:
                for chunk in self.split_by_size(other_files):
                    version_hash = self.compute_version_hash_from_files(chunk)
                    archive_name = f"common_{version_hash}"
                    self.create_zip_archive(archive_name, chunk, archive_name)

            if texture_files:
                for chunk in self.split_by_size(texture_files):
                    version_hash = self.compute_version_hash_from_files(chunk)
                    archive_name = f"common_texture_{version_hash}"
                    self.create_zip_archive(archive_name, chunk, archive_name)

    def create_collection_archives(self):
        print("Starting zip file collection")
        for path in self.zip_files:
            zip_file_name = os.path.splitext(
                os.path.basename(self.files[path]["children"][0])
            )[0]
            files_list = list(self.zip_files[path].keys())
            texture_files = [f for f in files_list if self.is_texture_resource(f)]
            other_files = [f for f in files_list if not self.is_texture_resource(f)]

            if other_files:
                self.create_zip_archive(zip_file_name, other_files)

            if texture_files:
                texture_archive_name = f"{zip_file_name}_texture"
                self.create_zip_archive(texture_archive_name, texture_files)
                main_file = self.files[path]["children"][0]
                if main_file not in self.dependency_list:
                    self.dependency_list[main_file] = []
                if texture_archive_name not in self.dependency_list[main_file]:
                    self.dependency_list[main_file].append(texture_archive_name)
        print("Zip file collection completed.")

    def build_manifest_output(self):
        collections_list = []
        collection_index = {}

        def intern_collection(name):
            idx = collection_index.get(name)
            if idx is None:
                idx = len(collections_list)
                collection_index[name] = idx
                collections_list.append(name)
            return idx

        archive_deps = {}
        for filepath, archives in self.dependency_list.items():
            file_name = os.path.splitext(os.path.basename(filepath))[0]
            for archive in archives:
                archive_deps.setdefault(archive, []).append(file_name)

        files = {}
        for archive_name, archive_info in self.created_archives.items():
            version_hash = archive_info["version_hash"]
            dep_files = archive_deps.get(archive_name)
            if dep_files:
                indices = [intern_collection(f) for f in dep_files]
                files[archive_name] = [version_hash, indices]
            else:
                files[archive_name] = [version_hash]

        return {
            "version": self.current_timestamp,
            "collections": collections_list,
            "files": files,
            "dmanifest_info": self.build_dmanifest_info(),
        }

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

        self.rename_archives_to_version()

        manifest_output = self.build_manifest_output()
        self.write_outputs(manifest_output)

        print("Process completed successfully.")


def main():
    PackContext().run()


if __name__ == "__main__":
    main()
