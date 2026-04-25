local log = {
	debug = function() end,
	warn = function(fmt, ...)
		print("[Liveupdater] " .. string.format(fmt, ...))
	end,
	error = function(fmt, ...)
		print("[Liveupdater ERROR] " .. string.format(fmt, ...))
	end,
}

---@alias liveupdater.res_mode integer
---@alias liveupdater.resolution "low"|"high"

---@class liveupdater.module
---@field files string[]
---@field priority number
---@field res_mode liveupdater.res_mode?
---@field by_request boolean?
---@field processed boolean?

---@class liveupdater.manifest
---@field version string
---@field collections string[]
---@field files table<string, [string, integer[]?]>

---@class liveupdater.manifest_group
---@field lowres liveupdater.manifest
---@field highres liveupdater.manifest?

---@class liveupdater.file_progress
---@field progress number
---@field prefix string

---@class liveupdater.queue_item
---@field file_name string
---@field priority number
---@field version string
---@field module_name string
---@field path string
---@field prefix string
---@field resolution liveupdater.resolution
---@field remount boolean?
---@field order integer?
---@field reason string?
---@field attempts integer?

---@class liveupdater.check_result
---@field files_to_remove table<string, string>

---@class liveupdater.module_file_progress
---@field name string
---@field progress number

---@class liveupdater.module_progress
---@field name string
---@field progress number
---@field files liveupdater.module_file_progress[]

---@class liveupdater.init_options
---@field modules table<string, liveupdater.module>
---@field lowres_server_path string
---@field hires_server_path string?
---@field disabled boolean?
---@field save_cache_key string?

---@class liveupdater
---@field initialized boolean
---@field saved_list table<string, string>
---@field modules table<string, liveupdater.module>
---@field modules_availability_fun fun(module_name: string): boolean
local M = {
	initialized = false,
	---@type table<string, string>
	saved_list = nil,
	---@type table<string, liveupdater.module>
	modules = nil,
}

M.MSG_INITIALIZED = hash("liveupdater_initialized")
M.MSG_INITIALIZATION_FAILED = hash("liveupdater_initialization_failed")
M.MSG_FILE_LOADED = hash("liveupdater_file_loaded")
M.MSG_MODULE_LOADED = hash("liveupdater_module_loaded")
M.MSG_ALL_LOADED = hash("liveupdater_all_loaded")
M.MSG_NETWORK_ERROR = hash("liveupdater_network_error")

---@type table<string, url>
local event_listeners = {}

local COMMON_MODULE = "COMMON"
local FILE_EXTENSION = ".arcd0"

local EMPTY_HASH = hash("")

---@param id hash|string
---@return hash
local function ensure_hash(id)
	return type(id) == "string" and hash(id) or id --[[@as hash]]
end

---@param url url
---@return string
local function url_to_key(url)
	return hash_to_hex(url.socket or EMPTY_HASH)
		.. hash_to_hex(url.path or EMPTY_HASH)
		.. hash_to_hex(url.fragment or EMPTY_HASH)
end

---@param file_name string
---@param version string
---@return string
local function build_archive_filename(file_name, version)
	return file_name .. "_" .. version .. FILE_EXTENSION
end

local RES_HIGH_ONLY = 1
local RES_LOW_ONLY = 2
local RES_BOTH = 3

M.RES_HIGH_ONLY = RES_HIGH_ONLY
M.RES_LOW_ONLY = RES_LOW_ONLY
M.RES_BOTH = RES_BOTH

local LOWRES_PREFIX = "lowres_"
local HIGH_PREFIX = "high_"
local NOTLOAD_PREFIX = "notloaded_"
local FROMLOCAL_PREFIX = "local_"

---@type string?
local highres_path
---@type boolean
local has_highres = false
---@type string
local low_res_path
---@type string
local manifest_json = "manifest.json"
---@type string
local local_folder = "liveupdate_zip"

---@type integer
local max_attempts = 3
---@type integer
local max_item_attempts = 3
---@type number
local retry_delay = 5

---@type table<string, liveupdater.file_progress>
local downloadable_files = {}
---@type table<string, string>
local downloadable_missing_files = {}

---@type liveupdater.manifest_group
local global_manifest = {}
---@type liveupdater.check_result?
local check_result = nil

---@type liveupdater.queue_item[]
local download_files_queue = {}
---@type table<string, liveupdater.queue_item>
local download_queue_index = {}
---@type integer
local download_queue_order = 0

local files_load_completed = false
---@type table<string, table<string, boolean>>
local dependency_map_low = {}
---@type table<string, table<string, boolean>>
local dependency_map_high = {}

---@return boolean
local function is_liveupdate_available()
	return liveupdate.is_built_with_excluded_files() and not M.disabled
end

---@param message_id hash
---@param message table?
local function notify_event_listeners(message_id, message)
	for _, url in pairs(event_listeners) do
		msg.post(url, message_id, message or {})
	end
end

---@param filename string
---@return string
local function get_local_path(filename)
	return sys.get_save_file(local_folder, filename)
end

---@param filename string
---@param data string
---@return boolean
---@return string?
local function save_file(filename, data)
	local path = get_local_path(filename)
	local file, err = io.open(path, "w+")
	if err or not file then
		return false, err
	end
	local f, err = file:write(data)
	if err then
		return false, err
	end
	file:close()
	return true, nil
end

local function save_local_files_list()
	local data = {
		saved_list = M.saved_list,
	}
	local success, err = pcall(sys.save, M.save_cache_key, data)
	if not success then
		log.error("Failed to save local files list: %s", err)
		return
	end
end

local function load_local_files_list()
	if not M.saved_list then
		local data = sys.load(M.save_cache_key)
		M.saved_list = data.saved_list or {}
	end
end

---@param file_name string
---@param version string?
local function set_saved_file_version(file_name, version)
	M.saved_list[file_name] = version
	save_local_files_list()
end

---@param filename string
---@return boolean
local function file_exists(filename)
	return sys.exists(get_local_path(filename))
end

---@param filename string
---@return boolean
---@return string?
local function remove_local_file(filename)
	if sys.exists(get_local_path(filename)) then
		return os.remove(get_local_path(filename))
	else
		return true, nil
	end
end

---@param data string?
---@return table?
local function decode_json_data(data)
	if not data then
		return nil
	end
	local parsed, response_data = pcall(json.decode, data)
	if parsed then
		return response_data
	end
end

---@param filename string
---@return boolean
local function is_mount_exist(filename)
	for _, mount in ipairs(liveupdate.get_mounts()) do
		if mount.name == filename then
			return true
		end
	end
	return false
end

---@return integer
local function get_next_priority()
	local max_priority = 0
	for _, mount in ipairs(liveupdate.get_mounts()) do
		if mount.priority > max_priority then
			max_priority = mount.priority
		end
	end
	return max_priority + 1
end

---@param filename string
---@param cb fun(success: boolean)?
local function mount_resource(filename, cb)
	if is_mount_exist(filename) then
		if liveupdate.remove_mount(filename) ~= liveupdate.LIVEUPDATE_OK then
			error("Unable to remove mount " .. filename)
		end
	end

	liveupdate.add_mount(
		filename,
		"zip:" .. get_local_path(filename),
		get_next_priority(),
		function(self, path, uri, result)
			if cb then
				cb(result == liveupdate.LIVEUPDATE_OK)
			end
		end
	)
end

---@param prefix string
---@param filename string
---@param bytes_received number
---@param bytes_total number
local function update_file_progress(prefix, filename, bytes_received, bytes_total)
	local data = {
		progress = (bytes_received / bytes_total) * 100,
		prefix = prefix,
	}

	downloadable_files[filename] = data
end

---@param prefix string
---@param path string
---@param filename string
---@param basename string
---@param cb fun(success: boolean, data: string?)
---@param attempt integer?
---@param version string?
local function request_data(prefix, path, filename, basename, cb, attempt, version)
	attempt = attempt or 1
	version = version or tostring(math.floor(socket.gettime()))
	http.request(
		path .. filename .. "?" .. version,
		"GET",
		function(self, id, response)
			if
				(response.status == 200 or response.status == 304)
				and response.error == nil
				and response.response ~= nil
			then
				cb(true, response.response)
			elseif attempt <= max_attempts then
				attempt = attempt + 1
				request_data(prefix, path, filename, basename, cb, attempt, version)
			else
				cb(false, response.error)
			end
		end,
		nil,
		nil,
		{
			timeout = 200,
		}
	)
end

---@param manifest liveupdater.manifest?
---@param file_name string
---@return string?
local function get_file_version(manifest, file_name)
	local entry = manifest and manifest.files and manifest.files[file_name]
	return entry and entry[1]
end

---@param module liveupdater.module
---@return liveupdater.res_mode
local function get_res_mode(module)
	return module.res_mode or RES_HIGH_ONLY
end

---@param collections string[]
---@return table<string, boolean>
local function build_collection_set(collections)
	local result = {}
	for file_name, _ in pairs(collections) do
		result[file_name] = true
	end
	return result
end

---@param manifest liveupdater.manifest?
---@return table<string, boolean>
local function build_dep_set(manifest)
	local result = {}
	for name, entry in pairs(manifest and manifest.files or {}) do
		if entry[2] then
			result[name] = true
		end
	end
	return result
end

---@param manifest liveupdater.manifest?
---@return table<string, table<string, boolean>>
local function build_dependency_map(manifest)
	local result = {}
	local collections = manifest and manifest.collections or {}
	for archive_name, entry in pairs(manifest and manifest.files or {}) do
		local dep_indices = entry[2]
		if dep_indices then
			for _, idx in ipairs(dep_indices) do
				local filename = collections[idx + 1]
				if filename then
					if not result[filename] then
						result[filename] = {}
					end
					result[filename][archive_name] = true
				end
			end
		end
	end
	return result
end

---@param dep_map table<string, table<string, boolean>>
---@param file_name string
---@return string[]
local function get_dependencies(dep_map, file_name)
	local deps = dep_map[file_name]
	if not deps then
		return {}
	end
	local result = {}
	for dep_name, _ in pairs(deps) do
		table.insert(result, dep_name)
	end
	return result
end

---@param cb fun(success: boolean, result: liveupdater.manifest_group|string)
local function check_manifests(cb)
	request_data(LOWRES_PREFIX, low_res_path, manifest_json, manifest_json, function(success, data)
		if not success then
			cb(false, "Unable to get low res manifest")
			return
		end
		local lowres_remote_manifest = decode_json_data(data)
		if not lowres_remote_manifest then
			cb(false, "Unable to parse low res manifest")
			return
		end
		if not has_highres then
			cb(true, { lowres = lowres_remote_manifest, highres = nil })
			return
		end
		request_data(HIGH_PREFIX, highres_path, manifest_json, manifest_json, function(success, data)
			if not success then
				cb(false, "Unable to get highres manifest")
				return
			end
			local highres_remote_manifest = decode_json_data(data)
			if not highres_remote_manifest then
				cb(false, "Unable to parse highres manifest")
				return
			end
			cb(true, { lowres = lowres_remote_manifest, highres = highres_remote_manifest })
		end)
	end)
end

---@param file_name string
---@return boolean
local function remove_mount_and_local_file(file_name)
	for _, mount in ipairs(liveupdate.get_mounts()) do
		if mount.name == file_name then
			if liveupdate.remove_mount(file_name) == liveupdate.LIVEUPDATE_OK then
				break
			else
				log.error("Unable to remove mount %s", file_name)
				return false
			end
		end
	end
	local result, err = remove_local_file(file_name)
	if not result then
		log.error("Unable to remove file %s: %s", file_name, err)
		return false
	end
	set_saved_file_version(file_name, nil)
	return true
end

---@param manifest liveupdater.manifest_group
---@param callback fun(success: boolean)
local function mount_existing_valid_files(manifest, callback)
	local saved_count = 0
	for _ in pairs(M.saved_list) do
		saved_count = saved_count + 1
	end
	local low_manifest = manifest.lowres
	local high_manifest = manifest.highres
	local low_set = build_collection_set(low_manifest.files)
	local high_set = build_collection_set(high_manifest and high_manifest.files or {})
	local low_dep_set = build_dep_set(low_manifest)
	local high_dep_set = build_dep_set(high_manifest)

	local to_mount = {}
	local invalid_saved = {}
	for filename, saved_version in pairs(M.saved_list) do
		if low_set[filename] or high_set[filename] or low_dep_set[filename] or high_dep_set[filename] then
			local low_version = (low_set[filename] or low_dep_set[filename])
					and get_file_version(low_manifest, filename)
				or nil
			local high_version = (high_set[filename] or high_dep_set[filename])
					and get_file_version(high_manifest, filename)
				or nil
			local exists = file_exists(filename)
			if (saved_version == low_version or (high_version and saved_version == high_version)) and exists then
				table.insert(to_mount, filename)
			else
				table.insert(invalid_saved, filename)
			end
		end
	end

	for _, filename in ipairs(invalid_saved) do
		if not remove_mount_and_local_file(filename) then
			log.warn("Unable to cleanup invalid saved file %s", filename)
		end
	end

	if #to_mount == 0 then
		callback(true)
		return
	end

	local function mount_next(index)
		if index > #to_mount then
			callback(true)
			return
		end
		local filename = to_mount[index]
		mount_resource(filename, function(success)
			if not success then
				log.error("Mount existing files error: %s", filename)
				callback(false)
				return
			end
			mount_next(index + 1)
		end)
	end

	mount_next(1)
end

---@param low_manifest liveupdater.manifest
---@param high_manifest liveupdater.manifest?
---@param low_collection_set table<string, boolean>
---@param high_collection_set table<string, boolean>
---@return liveupdater.check_result
local function classify_files(low_manifest, high_manifest, low_collection_set, high_collection_set)
	local result = {
		files_to_remove = {},
	}

	for filename, saved_version in pairs(M.saved_list) do
		local low_version = low_collection_set[filename] and get_file_version(low_manifest, filename)
		local high_version = high_collection_set[filename] and get_file_version(high_manifest, filename)
		if not low_version and not high_version or (saved_version ~= low_version and saved_version ~= high_version) then
			result.files_to_remove[filename] = saved_version
		end
	end

	return result
end

---@param modules table<string, liveupdater.module>
---@param low_manifest liveupdater.manifest
---@param high_manifest liveupdater.manifest?
local function enqueue_modules(modules, low_manifest, high_manifest)
	local low_deps = dependency_map_low
	local high_deps = dependency_map_high

	---@param item liveupdater.queue_item
	---@param reason string
	local function add_to_queue(item, reason)
		local key = item.file_name .. ":" .. item.version
		local existing = download_queue_index[key]
		if existing then
			if existing.priority <= item.priority then
				return
			end
			-- new item has higher priority (smaller value) — drop the old entry
			for index, value in ipairs(download_files_queue) do
				if value == existing then
					table.remove(download_files_queue, index)
					break
				end
			end
		end
		download_queue_order = download_queue_order + 1
		item.order = download_queue_order
		item.reason = reason
		download_queue_index[key] = item
		table.insert(download_files_queue, item)
	end

	---@param dep_map table<string, table<string, boolean>>
	---@param file_name string
	---@param base_priority number
	---@param resolution liveupdater.resolution
	---@param path string
	---@param prefix string
	local function add_dependencies(dep_map, file_name, base_priority, resolution, path, prefix)
		local deps = get_dependencies(dep_map, file_name)
		for _, dep_name in ipairs(deps) do
			local saved_dep_version = M.saved_list[dep_name]
			local dep_version_low = get_file_version(low_manifest, dep_name)
			local dep_version_high = get_file_version(high_manifest, dep_name)
			local skip_dep = false
			if
				saved_dep_version
				and resolution == "low"
				and (saved_dep_version == dep_version_low or saved_dep_version == dep_version_high)
			then
				skip_dep = true
			end
			if saved_dep_version and resolution == "high" and (saved_dep_version == dep_version_high) then
				skip_dep = true
			end
			if not skip_dep then
				local version = resolution == "high" and dep_version_high or dep_version_low
				if version then
					add_to_queue({
						file_name = dep_name,
						priority = base_priority - 0.5,
						version = version,
						module_name = COMMON_MODULE,
						path = path,
						prefix = prefix,
						resolution = resolution,
						remount = resolution == "high",
					}, string.format("dependency_of=%s", file_name))
				end
			end
		end
	end

	for module_name, module in pairs(modules) do
		local available = true
		if module.by_request then
			available = false
		elseif M.modules_availability_fun and not M.modules_availability_fun(module_name) then
			available = false
		end

		if available then
			module.processed = true
			for _, file_name in ipairs(module.files) do
				local saved_version = M.saved_list[file_name]
				local low_version = get_file_version(low_manifest, file_name)
				local high_version = get_file_version(high_manifest, file_name)
				local res_mode = get_res_mode(module)
				local supports_high = has_highres and (res_mode == RES_HIGH_ONLY or res_mode == RES_BOTH)
				local supports_low = not supports_high or res_mode == RES_LOW_ONLY or res_mode == RES_BOTH

				local needs_update = not saved_version
					or (saved_version ~= low_version and saved_version ~= high_version)

				if supports_low then
					add_dependencies(low_deps, file_name, module.priority, "low", low_res_path, LOWRES_PREFIX)
					if low_version and needs_update then
						add_to_queue({
							file_name = file_name,
							priority = module.priority,
							version = low_version,
							module_name = module_name,
							path = low_res_path,
							prefix = LOWRES_PREFIX,
							resolution = "low",
						}, "needs_low")
					end
				end

				if supports_high then
					add_dependencies(high_deps, file_name, module.priority + 1000, "high", highres_path, HIGH_PREFIX)
					if high_version and needs_update and (not supports_low or high_version ~= low_version) then
						add_to_queue({
							file_name = file_name,
							priority = module.priority + 1000,
							version = high_version,
							module_name = module_name,
							path = highres_path,
							prefix = HIGH_PREFIX,
							resolution = "high",
							remount = true,
						}, "needs_high")
					end
				end
			end
		end
	end

	table.sort(download_files_queue, function(a, b)
		if a.priority == b.priority then
			return a.order < b.order
		end
		return a.priority < b.priority
	end)
end

---@param load_info liveupdater.queue_item
local function remove_mount(load_info)
	if load_info.remount then
		if is_mount_exist(load_info.file_name) then
			if liveupdate.remove_mount(load_info.file_name) ~= liveupdate.LIVEUPDATE_OK then
				log.error("Unable to remove mount %s", load_info.file_name)
			end
		end
		local remove_local_result, err = remove_local_file(load_info.file_name)
		if not remove_local_result then
			log.error("Unable to remove file %s: %s", load_info.file_name, err)
		end
		set_saved_file_version(load_info.file_name, nil)
	end
end

---@param modules table<string, liveupdater.module>
---@param manifest liveupdater.manifest_group
local function check_modules_integrity(modules, manifest)
	local all_module_files = {}

	for _, module in pairs(modules) do
		for _, file_name in ipairs(module.files) do
			all_module_files[file_name] = true
		end
	end

	local reference_manifest = manifest.highres or manifest.lowres
	local highres_collections = {}
	for file_name, _ in pairs(reference_manifest.files) do
		highres_collections[file_name] = true
	end
	local highres_deps = build_dep_set(reference_manifest)

	for file_name, _ in pairs(all_module_files) do
		if not highres_collections[file_name] then
			local error_text =
				string.format("The module specifies a file that is not listed in the manifest. Module: %s", file_name)
			log.warn(error_text)
			downloadable_missing_files[file_name] = error_text
		end
	end

	for file_name, _ in pairs(highres_collections) do
		if all_module_files[file_name] == nil and not highres_deps[file_name] then
			local error_text =
				string.format("The manifest lists a file that is not present in the modules. File: %s", file_name)
			log.warn(error_text)
			downloadable_missing_files[file_name] = error_text
		end
	end
end

---@param load_info liveupdater.queue_item
local function drop_queue_item(load_info)
	download_queue_index[load_info.file_name .. ":" .. load_info.version] = nil
	for index, value in ipairs(download_files_queue) do
		if value == load_info then
			table.remove(download_files_queue, index)
			break
		end
	end
end

---@param load_info liveupdater.queue_item
---@param load_resources fun()
---@param reason string
local function schedule_retry_or_drop(load_info, load_resources, reason)
	load_info.attempts = (load_info.attempts or 0) + 1
	if load_info.attempts >= max_item_attempts then
		log.error("Giving up on %s after %d attempts (%s)", load_info.file_name, max_item_attempts, reason)
		downloadable_missing_files[load_info.file_name] =
			string.format("Failed after %d attempts: %s", max_item_attempts, reason)
		drop_queue_item(load_info)
		load_resources()
	else
		timer.delay(retry_delay, false, load_resources)
	end
end

---@param load_info liveupdater.queue_item
---@param load_resources fun()
local function add_mount(load_info, load_resources)
	mount_resource(load_info.file_name, function(success)
		if success then
			update_file_progress(load_info.prefix, load_info.file_name, 1, 1)
			set_saved_file_version(load_info.file_name, load_info.version)

			download_queue_index[load_info.file_name .. ":" .. load_info.version] = nil
			for index, value in ipairs(download_files_queue) do
				if value.file_name == load_info.file_name and value.version == load_info.version then
					table.remove(download_files_queue, index)
					break
				end
			end

			notify_event_listeners(M.MSG_FILE_LOADED, {
				file_name = load_info.file_name,
				module_name = load_info.module_name,
			})
			if load_info.module_name ~= COMMON_MODULE and M.is_module_loaded(load_info.module_name) then
				notify_event_listeners(M.MSG_MODULE_LOADED, {
					file_name = load_info.file_name,
					module_name = load_info.module_name,
				})
			end

			if next(download_files_queue) then
				load_resources()
			else
				files_load_completed = true
				notify_event_listeners(M.MSG_ALL_LOADED)
				log.debug("All global files loaded")
			end
		else
			log.error("Unable to mount file %s", load_info.file_name)
			schedule_retry_or_drop(load_info, load_resources, "mount failed")
		end
	end)
end

---@param success boolean
---@param data string?
---@param load_info liveupdater.queue_item
---@param load_resources fun()
local function handle_load_info_callback(success, data, load_info, load_resources)
	if success then
		remove_mount(load_info)
		local result, err = save_file(load_info.file_name, data)
		if result then
			add_mount(load_info, load_resources)
		else
			log.error("Unable to save file %s: %s", load_info.file_name, err)
			schedule_retry_or_drop(load_info, load_resources, string.format("save failed: %s", tostring(err)))
		end
	else
		notify_event_listeners(M.MSG_NETWORK_ERROR)
		schedule_retry_or_drop(load_info, load_resources, "network error")
	end
end

local function try_start_load_resources()
	---@type liveupdater.queue_item?
	local load_info = download_files_queue[1]
	if load_info then
		---@type fun(success: boolean, data: string?)?
		local callback = nil

		callback = function(success, data)
			handle_load_info_callback(success, data, load_info, try_start_load_resources)
		end

		request_data(
			load_info.prefix,
			load_info.path,
			build_archive_filename(load_info.file_name, load_info.version),
			load_info.file_name,
			callback,
			nil,
			load_info.version
		)
	else
		files_load_completed = true
	end
end

---@param success boolean
---@param result liveupdater.check_result|string
---@param cb fun(success: boolean, error_text: string?)
---@param actual_manifest liveupdater.manifest_group
---@param low_collection_set table<string, boolean>
---@param high_collection_set table<string, boolean>
local function cleanup_and_initialize_web(success, result, cb, actual_manifest, low_collection_set, high_collection_set)
	if success then
		dependency_map_low = build_dependency_map(actual_manifest.lowres)
		dependency_map_high = build_dependency_map(actual_manifest.highres)

		for file_name, _ in pairs(result.files_to_remove) do
			local remove_success, err = remove_mount_and_local_file(file_name)
			if not remove_success then
				log.error("Unable to remove file from storage %s: %s", file_name, err)
				cb(false, "Unable to remove file " .. file_name)
				return
			end
		end

		mount_existing_valid_files(actual_manifest, function(success)
			if not success then
				cb(false, "Unable to mount existing valid files")
				return
			end
			download_files_queue = {}
			download_queue_index = {}
			download_queue_order = 0
			enqueue_modules(M.modules, actual_manifest.lowres, actual_manifest.highres)

			check_modules_integrity(M.modules, actual_manifest)

			for filename, _ in pairs(M.saved_list) do
				update_file_progress(FROMLOCAL_PREFIX, filename, 1, 1)
			end

			try_start_load_resources()

			M.initialized = true
			notify_event_listeners(M.MSG_INITIALIZED)
			cb(true)
		end)
	else
		cb(false, result)
		notify_event_listeners(M.MSG_INITIALIZATION_FAILED)
	end
end

---@param module_name string
---@return boolean
function M.has_module(module_name)
	assert(M.initialized, "Liveupdater: not initialized")
	assert(M.modules, "Liveupdater: modules not initialized")
	return M.modules[module_name] ~= nil
end

---@param module_name string
---@return boolean
function M.is_module_loaded(module_name)
	assert(M.initialized, "Liveupdater: not initialized")
	assert(M.modules, "Liveupdater: modules not initialized")
	local module = M.modules[module_name]
	assert(module, "Liveupdater: No such module " .. tostring(module_name))

	if not is_liveupdate_available() then
		return true
	end

	local res_mode = get_res_mode(module)
	local supports_high = has_highres and (res_mode == RES_HIGH_ONLY or res_mode == RES_BOTH)
	local supports_low = not supports_high or res_mode == RES_LOW_ONLY or res_mode == RES_BOTH

	for _, value in ipairs(module.files) do
		local saved_version = M.saved_list[value]
		if not saved_version or not file_exists(value) then
			return false
		end

		local low_version = get_file_version(global_manifest.lowres, value)
		local high_version = get_file_version(global_manifest.highres, value)

		local matches_low = supports_low and saved_version == low_version
		local matches_high = supports_high and saved_version == high_version
		if not matches_low and not matches_high then
			return false
		end

		local dep_map = matches_high and dependency_map_high or dependency_map_low
		local deps = dep_map[value] or {}
		for dep_name, _ in pairs(deps) do
			local dep_saved_version = M.saved_list[dep_name]
			if not dep_saved_version then
				return false
			end

			local dep_low_version = get_file_version(global_manifest.lowres, dep_name)
			local dep_high_version = get_file_version(global_manifest.highres, dep_name)
			local dep_valid = dep_saved_version == dep_high_version or dep_saved_version == dep_low_version
			if not dep_valid then
				return false
			end
		end
	end
	return true
end

---@param options liveupdater.init_options
---@param cb fun(success: boolean, error_text: string?)
---@param modules_availability_fun fun(module_name: string): boolean
function M.init(options, cb, modules_availability_fun)
	M.modules = options.modules
	M.modules_availability_fun = modules_availability_fun
	M.disabled = options.disabled
	M.save_cache_key = options.save_cache_key
	if not is_liveupdate_available() then
		M.initialized = true
		notify_event_listeners(M.MSG_INITIALIZED)
		cb(true)
		return
	end
	load_local_files_list()

	assert(options, "Liveupdater: options not provided")
	assert(options.lowres_server_path, "Liveupdater: lowres_server_path not provided")
	highres_path = options.hires_server_path
	has_highres = highres_path ~= nil
	low_res_path = options.lowres_server_path

	if html5 then
		check_manifests(function(global_success, result)
			global_manifest = result
			if global_success then
				local low_collection_set = build_collection_set(global_manifest.lowres.files)
				local high_collection_set =
					build_collection_set(global_manifest.highres and global_manifest.highres.files or {})
				check_result = classify_files(
					global_manifest.lowres,
					global_manifest.highres,
					low_collection_set,
					high_collection_set
				)
				cleanup_and_initialize_web(
					global_success,
					check_result,
					cb,
					global_manifest,
					low_collection_set,
					high_collection_set
				)
			else
				cb(false, string.format("Failed. Global_success: %s", tostring(global_success)))
			end
		end)
	end
end

---@param logger { debug: fun(fmt: string, ...), warn: fun(fmt: string, ...), error: fun(fmt: string, ...) }
function M.set_logger(logger)
	log = logger
end

---@param url url?
function M.add_listener(url)
	url = url or msg.url()
	event_listeners[url_to_key(url)] = url
end

---@param url url?
function M.remove_listener(url)
	url = url or msg.url()
	event_listeners[url_to_key(url)] = nil
end

---@param module_name string
---@param module liveupdater.module?
---@return liveupdater.module_progress?
function M.get_module_progress(module_name, module)
	if not module then
		log.warn("Module not found")
		return nil
	end

	local module_info = {
		name = module_name,
		progress = 0,
		files = {},
	}

	local module_loaded = 0
	local module_total = #module.files
	for _, file_name in ipairs(module.files) do
		local file_info = downloadable_files[file_name]
		if file_info then
			local prefix_filename = file_info.prefix .. file_name
			local file_progress = file_info and file_info.progress or 0
			table.insert(module_info.files, {
				name = prefix_filename,
				progress = file_progress,
			})

			if file_progress == 100 then
				module_loaded = module_loaded + 1
			end
		else
			local prefix_filename = NOTLOAD_PREFIX .. file_name
			local file_progress = 0
			table.insert(module_info.files, {
				name = prefix_filename,
				progress = file_progress,
			})
		end
	end

	if module_loaded > 0 then
		module_info.progress = (module_loaded / module_total) * 100
	else
		module_info.progress = 0
	end

	return module_info
end

---@return boolean
function M.is_liveupdate_loaded()
	return M.initialized and not next(download_files_queue)
end

---@return table<string, liveupdater.file_progress>
function M.get_downloadable_files()
	return downloadable_files
end

---@return table<string, string>
function M.get_downloadable_missing_files()
	return downloadable_missing_files
end

---@return number
function M.get_total_downloadable_progress()
	if not is_liveupdate_available() then
		return 0
	end
	local total_progress = 0
	local module_count = 0

	for module_name, module in pairs(M.modules) do
		local module_info = M.get_module_progress(module_name, module)
		if module_info then
			total_progress = total_progress + module_info.progress
			module_count = module_count + 1
		end
	end

	if module_count == 0 then
		return 0
	end

	return total_progress / module_count
end

---@param module_name string
---@return boolean?
function M.is_module_in_queue(module_name)
	if not is_liveupdate_available() then
		return true
	end
	local module = M.modules[module_name]
	return module and module.processed
end

---@param module_name string
function M.request_module_load(module_name)
	if not M.initialized then
		return
	end
	if not is_liveupdate_available() then
		return
	end
	local module = M.modules[module_name]
	if not module then
		return
	end
	if module.by_request and not module.processed then
		module.by_request = false
		enqueue_modules({ [module_name] = module }, global_manifest.lowres, global_manifest.highres)
		if files_load_completed then
			files_load_completed = false
			try_start_load_resources()
		end
	end
end

return M
