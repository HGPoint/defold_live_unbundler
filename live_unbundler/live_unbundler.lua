---@generic T
---@param obj T
---@param deep boolean?
---@return T
local function copy_table(obj, deep)
	if type(obj) ~= "table" then
		return obj
	end
	local result = {}
	for k, v in pairs(obj) do
		if type(v) == "table" and deep then
			result[k] = copy_table(v, deep)
		else
			result[k] = v
		end
	end
	return result
end

local EMPTY_HASH = hash("")

---@param url url
---@return string
local function url_to_key(url)
	return hash_to_hex(url.socket or EMPTY_HASH)
		.. hash_to_hex(url.path or EMPTY_HASH)
		.. hash_to_hex(url.fragment or EMPTY_HASH)
end

---@alias liveupdater.res_mode integer
---@alias liveupdater.resolution "low"|"high"

---@class liveupdater.module
---@field files string[]
---@field priority number
---@field res_mode liveupdater.res_mode?
---@field by_request boolean?
---@field processed boolean?
---@field focus_low number? set by M.focus, overrides priority for the low resolution
---@field focus_high number? set by M.focus, overrides priority for the high resolution

---@class liveupdater.file_version_info
---@field version string
---@field size integer?

---@class liveupdater.manifest
---@field version string
---@field deps table<string, string[]>
---@field file_versions table<string, liveupdater.file_version_info|string>
---@field file_sizes table<string, integer>?
---@field file_md5 table<string, string>? chunked md5 of each .arcd0; absent → no cache check
---@field md5_chunk_size integer? chunk size of file_md5: hashmd5 of the concatenated hex md5 of each chunk
---@field file_md5_size table<string, integer>? bytes covered by file_md5: the archive up to its dmanifest entry
---@field merged_archives table<string, string[]>? pack name -> member archive names

---@class liveupdater.manifest_group
---@field lowres liveupdater.manifest? present only when the lowres resolution is active
---@field highres liveupdater.manifest? present only when the highres resolution is active

---Normalized view of one remote manifest, built once per init.
---@class liveupdater.manifest_side
---@field versions table<string, string> archive name -> version hash
---@field sizes table<string, integer> archive name -> archive size in bytes (0 if the manifest has no sizes)
---@field md5 table<string, string> archive name -> chunked md5 of .arcd0 (empty when manifest has no file_md5)
---@field md5_chunk_size integer
---@field md5_size table<string, integer> archive name -> bytes covered by md5
---@field merged_into table<string, string> member archive name -> pack name
---@field pack_set table<string, boolean> set of pack archive names from merged_archives
---@field dep_lists table<string, string[]> collection file -> dependency archive names
---@field dep_set table<string, boolean> set of all dependency archive names
---@field path string server path for this resolution
---@field prefix string progress prefix for this resolution

---A resolution side is present only when its server path was passed to M.init.
---At least one of the two is always present.
---@class liveupdater.manifest_cache
---@field low liveupdater.manifest_side?
---@field high liveupdater.manifest_side?

---@class liveupdater.file_progress
---@field progress number
---@field prefix string

---@class liveupdater.queue_item
---@field file_name string
---@field priority number
---@field version string
---@field size integer
---@field module_name string
---@field path string
---@field prefix string
---@field resolution liveupdater.resolution
---@field remount boolean?
---@field order integer?
---@field reason string?
---@field download_class integer? M.DOWNLOAD_CLASS.*, set when the item is queued
---@field verify_attempts integer?
---@field mount_attempts integer?
---@field retry_at number?

---@class liveupdater.module_file_progress
---@field name string
---@field progress number

---@class liveupdater.module_progress
---@field name string
---@field progress number
---@field files liveupdater.module_file_progress[]

---@class liveupdater.resolution_progress
---@field total_bytes integer
---@field downloaded_bytes integer
---@field pending_files integer
---@field progress number 0..100, reaches 100 exactly when nothing is left to download

---@class liveupdater.download_progress: liveupdater.resolution_progress
---@field low liveupdater.resolution_progress
---@field high liveupdater.resolution_progress
---@field gate liveupdater.resolution_progress
---@field downloading liveupdater.active_download[]

---@class liveupdater.active_download
---@field key string
---@field size number
---@field bytes number
---@field in_gate boolean

---At least one of lowres_server_path / hires_server_path must be provided. When
---only one is provided that resolution is the only active one: res_mode on the
---modules is ignored and every module loads that single resolution.
---@class liveupdater.init_options
---@field modules table<string, liveupdater.module>
---@field lowres_server_path string?
---@field hires_server_path string?

---@class liveupdater
---@field initialized boolean
---@field saved_list table<string, string>
---@field save_cache_key string
---@field modules table<string, liveupdater.module>
local M = {
	initialized = false,
	---@type table<string, string>
	saved_list = nil,
	---@type string
	save_cache_key = nil,
	---@type table<string, liveupdater.module>
	modules = nil,
}

M.MSG_INITIALIZED = hash("liveupdater_initialized")
M.MSG_INITIALIZATION_FAILED = hash("liveupdater_initialization_failed")
M.MSG_FILE_LOADED = hash("liveupdater_file_loaded")
M.MSG_MODULE_LOADED = hash("liveupdater_module_loaded")
M.MSG_ALL_LOADED = hash("liveupdater_all_loaded")
M.MSG_NETWORK_ERROR = hash("liveupdater_network_error")

-- Log levels handed to the injected log function. Also exposed as a
-- level -> name lookup (liveupdater[liveupdater.DEBUG] == "DEBUG").
M.DEBUG, M[1] = 1, "DEBUG"
M.INFO, M[2] = 2, "INFO"
M.WARN, M[3] = 3, "WARN"
M.ERROR, M[4] = 4, "ERROR"
M.FATAL, M[5] = 5, "FATAL"

---@type fun(level: integer, message: string)
local log_function = function(_, _) end

---Assign a project logger. Signature: function(level, message), where level is
---one of M.DEBUG..M.FATAL. Optional — defaults to a no-op.
---@param fn fun(level: integer, message: string)
function M.set_log_function(fn)
	log_function = fn
end

---@param level integer
---@return fun(message: string, ...: any)
local function make_log_method(level)
	return function(message, ...)
		if select("#", ...) > 0 then
			local ok, formatted = pcall(string.format, message, ...)
			if ok then
				message = formatted
			end
		end
		log_function(level, message)
	end
end

local log = {
	debug = make_log_method(M.DEBUG),
	info = make_log_method(M.INFO),
	warn = make_log_method(M.WARN),
	error = make_log_method(M.ERROR),
	fatal = make_log_method(M.FATAL),
}

---Whether liveupdate may run on the current platform. Projects that must
---disable it (e.g. on Android) call M.set_platform_supported(false) before init.
---@type boolean
local platform_supported = true

---@param supported boolean
function M.set_platform_supported(supported)
	platform_supported = supported
end

---Sets the save-file key used to persist the list of downloaded archives. Must
---be called before M.init. The value is whatever sys.get_save_file(...) returns;
---keep it stable across releases so existing installs keep their saved state.
---@param key string
function M.set_save_cache_key(key)
	M.save_cache_key = key
end

---Predicate gating whether a non by_request module may be enqueued. Supplied
---via M.init; defaults to always-available.
---@type fun(module_name: string): boolean
local is_module_available = function()
	return true
end

---@type table<string, url>
local event_listeners = {}

local COMMON_MODULE = "COMMON"
local FILE_EXTENSION = ".arcd0"

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

M.PRIORITY_IDS = {
	BLOCKING = 1,
	FOCUS = 2,
	PREFETCH = 3,
	BACKGROUND = 4,
}

---Smaller numbers download earlier.
M.PRIORITY = {
	[M.PRIORITY_IDS.BLOCKING] = { low = 0, high = 500 },
	[M.PRIORITY_IDS.FOCUS] = { low = 1, high = 500 },
	[M.PRIORITY_IDS.PREFETCH] = { low = 600, high = 1500 },
	[M.PRIORITY_IDS.BACKGROUND] = { low = 2000, high = 3000 },
}

local HIGH_PRIORITY_OFFSET = 1000
local DEPENDENCY_PRIORITY_OFFSET = 0.5
local RETRY_DELAY = 5
local MAX_RETRY_DELAY = 60
local ZIP_URI_PREFIX = "zip:"

---Parallel download slot classes. A smaller id keeps the slot when two claims share a priority.
M.DOWNLOAD_CLASS = {
	BLOCKING = 1,
	BOOT = 2,
	OTHER = 3,
}

---Resolution ids. Values double as session_bytes and progress keys (.low / .high).
M.RESOLUTION = {
	LOW = "low",
	HIGH = "high",
}

---Module status, worst first: a module shows the smallest id across its files and dependencies.
M.STATUS = {
	ABSENT = 1,
	NOT_REQUESTED = 2,
	QUEUED = 3,
	DOWNLOADING = 4,
	MOUNTING = 5,
	READY = 6,
}

M.IN_FLIGHT_PHASE = {
	DOWNLOADING = M.STATUS.DOWNLOADING,
	MOUNTING = M.STATUS.MOUNTING,
}

local LOWRES_PREFIX = "lowres_"
local HIGH_PREFIX = "high_"
local NOTLOAD_PREFIX = "notloaded_"
local FROMLOCAL_PREFIX = "local_"

---@type string
local highres_path
---@type string
local low_res_path
---Which resolutions are active this session, decided by which server paths were
---passed to M.init. At least one is always true. When exactly one is active,
---res_mode on the modules is ignored and every module loads that single
---resolution; when both are active res_mode governs as before.
---@type boolean
local active_low = false
---@type boolean
local active_high = false
---@type string
local manifest_json = "manifest.json"
---@type string
local local_folder = "liveupdate_zip"

---@type integer
local max_attempts = 3

---@type integer
local parallel_downloads = 1
local PARALLEL_DOWNLOADS_MAX = 4
---@type boolean
local verify_hashes = true
local DISK_POST_QUEUE_LIMIT = 6

---@type table<string, liveupdater.file_progress>
local downloadable_files = {}
---@type table<string, string>
local downloadable_missing_files = {}

---@type liveupdater.manifest_cache?
local manifest_cache = nil

---@type liveupdater.queue_item[]
local download_files_queue = {}
---@type table<string, liveupdater.queue_item>
local download_queue_index = {}
---@type integer
local download_queue_order = 0

---@class liveupdater.in_flight_state
---@field item liveupdater.queue_item
---@field bytes integer
---@field phase integer M.IN_FLIGHT_PHASE.*

---@type table<string, liveupdater.in_flight_state>
local in_flight = {}

---@class liveupdater.mount_job
---@field load_info liveupdater.queue_item
---@field dl_name string?
---@field size_ok boolean?

---@type liveupdater.mount_job[]
local mount_queue = {}
---@type boolean
local mount_busy = false
---@type boolean
local mount_pump_scheduled = false

---@type table<string, boolean>
local mounting_files = {}

---@type table<string, string>
local remount_pending = {}

---@type table<string, boolean>
local pinned_archives = {}

---@type table<string, string[]>
local pinned_modules = {}

---@class liveupdater.staged_high
---@field load_info liveupdater.queue_item
---@field pending_name string
---@field mounted boolean

---@type table<string, liveupdater.staged_high>
local staged_high = {}

---@type liveupdater.staged_high[]
local promote_jobs = {}

---@type fun()?
local promote_on_done

local REMOUNT_PENDING_SUFFIX = ".__lu_pending"
local DOWNLOAD_TEMP_SUFFIX = ".__lu_dl"
local COPY_CHUNK_SIZE = 65536
local POST_BUDGET_SEC = 0.004
local POST_BUDGET_MIN_SEC = 0.001
local POST_BUDGET_MAX_SEC = 0.016

local bench_log_enabled = false

---@param enabled boolean
function M.set_bench_log(enabled)
	bench_log_enabled = enabled == true
end

---@param event string
---@param details string?
function M.bench_mark(event, details)
	if not bench_log_enabled then
		return
	end
	if details then
		print(string.format("LU_BENCH event=%s %s", event, details))
	else
		print(string.format("LU_BENCH event=%s", event))
	end
end

---@param phase string
---@param name string
---@param ms number
---@param bytes integer?
local function log_timing(phase, name, ms, bytes)
	log.debug("LU_TIMING phase=%s name=%s ms=%.1f", phase, name, ms)
	if not bench_log_enabled then
		return
	end
	if bytes and bytes > 0 then
		M.bench_mark("timing", string.format("phase=%s name=%s ms=%.1f bytes=%d", phase, name, ms, bytes))
	else
		M.bench_mark("timing", string.format("phase=%s name=%s ms=%.1f", phase, name, ms))
	end
end

---@param filename string
---@return string
local function download_temp_name(filename)
	return filename .. DOWNLOAD_TEMP_SUFFIX
end

---@type table<hash, {uri: string, priority: number}>
local mounts_by_hash = {}
---@type number
local max_mount_priority = 0

---@type table<string, table<string, {priority: number, class: string}>>
local queue_requesters = {}

---@param enabled boolean
function M.set_verify_hashes(enabled)
	verify_hashes = enabled ~= false
end

---@param count integer?
function M.set_parallel_downloads(count)
	if type(count) ~= "number" or count < 1 then
		parallel_downloads = 1
		return
	end
	parallel_downloads = math.min(math.floor(count), PARALLEL_DOWNLOADS_MAX)
end

---@return integer
function M.get_parallel_downloads()
	return parallel_downloads
end

---@param sec number?
function M.set_post_budget(sec)
	if type(sec) ~= "number" or sec ~= sec then
		POST_BUDGET_SEC = 0.004
		return
	end
	if sec < POST_BUDGET_MIN_SEC then
		POST_BUDGET_SEC = POST_BUDGET_MIN_SEC
	elseif sec > POST_BUDGET_MAX_SEC then
		POST_BUDGET_SEC = POST_BUDGET_MAX_SEC
	else
		POST_BUDGET_SEC = sec
	end
end

---@return number
function M.get_post_budget()
	return POST_BUDGET_SEC
end

---bytes queued and completed in the current download session, per resolution
---@type table<liveupdater.resolution, {total: integer, downloaded: integer}>
local session_bytes = {
	low = { total = 0, downloaded = 0 },
	high = { total = 0, downloaded = 0 },
}

local files_load_completed = false

---is_module_loaded results, valid until the next saved_list change
---@type table<string, boolean>
local module_loaded_memo = {}

---@type table<string, boolean>
local modules_announced_loaded = {}

---@return boolean
local function is_liveupdate_available()
	return liveupdate.is_built_with_excluded_files() and platform_supported
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
---@return string
local function zip_mount_uri(filename)
	return ZIP_URI_PREFIX .. get_local_path(filename)
end

---@param filename string
---@return boolean?, string?
local function remove_local_file(filename)
	if sys.exists(get_local_path(filename)) then
		return os.remove(get_local_path(filename))
	else
		return true, nil
	end
end

---@param from_name string
---@param to_name string
---@return boolean, string?
local function copy_local_file(from_name, to_name)
	local from_path = get_local_path(from_name)
	local to_path = get_local_path(to_name)
	local src, src_err = io.open(from_path, "rb")
	if not src then
		return false, src_err
	end
	local dst, dst_err = io.open(to_path, "wb")
	if not dst then
		src:close()
		return false, dst_err
	end
	while true do
		local chunk = src:read(COPY_CHUNK_SIZE)
		if not chunk then
			break
		end
		local ok, write_err = dst:write(chunk)
		if not ok then
			src:close()
			dst:close()
			return false, write_err
		end
	end
	src:close()
	dst:close()
	return true, nil
end

---@param from_name string
---@param to_name string
---@return boolean, string?
local function move_local_file(from_name, to_name)
	if from_name == to_name then
		return true, nil
	end
	remove_local_file(to_name)
	local from_path = get_local_path(from_name)
	local to_path = get_local_path(to_name)
	local renamed, rename_err = os.rename(from_path, to_path)
	if renamed then
		return true, nil
	end
	local copied, copy_err = copy_local_file(from_name, to_name)
	if not copied then
		return false, copy_err or rename_err
	end
	remove_local_file(from_name)
	return true, nil
end

---@class liveupdater.cache_md5_state
---@field md5 string
---@field ok boolean?
---@field failed boolean?
---@field retried boolean?

---@type table<string, liveupdater.cache_md5_state>
local cache_md5 = {}

---@type table<string, string>
local staged_versions = {}
local staged_restored = false

local function save_local_files_list()
	local data = {
		saved_list = M.saved_list,
		cache_md5 = cache_md5,
		staged_versions = staged_versions,
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
		cache_md5 = data.cache_md5 or {}
		staged_versions = data.staged_versions or {}
	end
end

---@param file_name string
---@param version string?
local function set_saved_file_version(file_name, version)
	M.saved_list[file_name] = version
	module_loaded_memo = {}
	save_local_files_list()
end

---@param filename string
---@return boolean
local function file_exists(filename)
	return sys.exists(get_local_path(filename))
end

---@param filename string
---@param saved_version string?
---@return string? md5
---@return integer? chunk_size
---@return integer? size
local function get_expected_md5(filename, saved_version)
	assert(manifest_cache, "Liveupdater: manifest cache not built")
	local accepted = {}
	local chunk_size = nil
	local size = nil
	for _, side in pairs({ manifest_cache.low, manifest_cache.high }) do
		local md5 = side.versions[filename] == saved_version and side.md5_size[filename] and side.md5[filename]
		if md5 and md5 ~= accepted[1] then
			accepted[#accepted + 1] = md5
			chunk_size = chunk_size or side.md5_chunk_size
			size = size or side.md5_size[filename]
		end
	end
	if not accepted[1] then
		return nil, nil, nil
	end
	table.sort(accepted)
	return table.concat(accepted, "|"), chunk_size, size
end

---@param expected string
---@param actual string
---@return boolean
local function md5_accepted(expected, actual)
	for md5 in string.gmatch(expected, "[^|]+") do
		if md5 == actual then
			return true
		end
	end
	return false
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

local function rebuild_mounts_index()
	mounts_by_hash = {}
	max_mount_priority = 0
	for _, mount in ipairs(liveupdate.get_mounts()) do
		mounts_by_hash[mount.name] = { uri = mount.uri, priority = mount.priority }
		if mount.priority > max_mount_priority then
			max_mount_priority = mount.priority
		end
	end
end

---@param filename string
---@return boolean
local function is_mount_exist(filename)
	return mounts_by_hash[hash(filename)] ~= nil
end

---@return integer
local function get_next_priority()
	max_mount_priority = max_mount_priority + 1
	return max_mount_priority
end

---@param filename string
local function unregister_mount(filename)
	mounts_by_hash[hash(filename)] = nil
end

---@param filename string
---@param uri string
---@param priority number
local function register_mount(filename, uri, priority)
	mounts_by_hash[hash(filename)] = { uri = uri, priority = priority }
	if priority > max_mount_priority then
		max_mount_priority = priority
	end
end

---@param filename string
---@return boolean
local function remove_engine_mount(filename)
	if not is_mount_exist(filename) then
		return true
	end
	if liveupdate.remove_mount(filename) ~= liveupdate.LIVEUPDATE_OK then
		log.error("Unable to remove mount %s", filename)
		return false
	end
	unregister_mount(filename)
	return true
end

---@param filename string
---@param cb fun(success: boolean)?
local function mount_resource(filename, cb)
	local uri = zip_mount_uri(filename)
	local existing = mounts_by_hash[hash(filename)]
	if existing and existing.uri == uri then
		if cb then
			cb(true)
		end
		return
	end
	if existing and not remove_engine_mount(filename) then
		error("Unable to remove mount " .. filename)
	end

	local priority = get_next_priority()
	liveupdate.add_mount(filename, uri, priority, function(_, _, _, result)
		local ok = result == liveupdate.LIVEUPDATE_OK
		if ok then
			register_mount(filename, uri, priority)
		end
		if cb then
			cb(ok)
		end
	end)
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
---@param progress_cb fun(bytes_received: number, bytes_total: number)?
local function request_data(prefix, path, filename, basename, cb, attempt, version, progress_cb)
	attempt = attempt or 1
	version = version or tostring(math.floor(socket.gettime()))
	http.request(
		path .. filename .. "?" .. version,
		"GET",
		function(self, id, response)
			-- with report_progress the callback also fires for transfer ticks;
			-- they carry bytes_* but no body, no error and no http status
			if
				progress_cb
				and response.bytes_total ~= nil
				and response.response == nil
				and response.error == nil
				and (response.status == nil or response.status == 0)
			then
				progress_cb(response.bytes_received or 0, response.bytes_total)
				return
			end
			if
				(response.status == 200 or response.status == 304)
				and response.error == nil
				and response.response ~= nil
			then
				cb(true, response.response)
			elseif attempt < max_attempts then
				request_data(prefix, path, filename, basename, cb, attempt + 1, version, progress_cb)
			else
				cb(false, response.error)
			end
		end,
		nil,
		nil,
		{
			timeout = 200,
			report_progress = progress_cb ~= nil,
		}
	)
end

---@param prefix string
---@param path string
---@param filename string
---@param basename string
---@param cb fun(success: boolean, dl_name: string?)
---@param attempt integer?
---@param version string?
---@param progress_cb fun(bytes_received: number, bytes_total: number)?
local function request_archive_file(prefix, path, filename, basename, cb, attempt, version, progress_cb)
	attempt = attempt or 1
	version = version or tostring(math.floor(socket.gettime()))
	local dl_name = download_temp_name(basename)
	local dest_path = get_local_path(dl_name)
	remove_local_file(dl_name)
	http.request(
		path .. filename .. "?" .. version,
		"GET",
		function(self, id, response)
			if
				progress_cb
				and response.bytes_total ~= nil
				and response.response == nil
				and response.error == nil
				and (response.status == nil or response.status == 0)
			then
				progress_cb(response.bytes_received or 0, response.bytes_total)
				return
			end
			local stored_ok = (response.status == 200 or response.status == 304)
				and response.error == nil
				and sys.exists(dest_path)
			if stored_ok then
				cb(true, dl_name)
			elseif attempt < max_attempts then
				remove_local_file(dl_name)
				request_archive_file(prefix, path, filename, basename, cb, attempt + 1, version, progress_cb)
			else
				remove_local_file(dl_name)
				cb(false, nil)
			end
		end,
		nil,
		nil,
		{
			timeout = 200,
			report_progress = progress_cb ~= nil,
			path = dest_path,
			ignore_cache = true,
		}
	)
end

---@param manifest liveupdater.manifest
---@param path string
---@param prefix string
---@return liveupdater.manifest_side
local function build_manifest_side(manifest, path, prefix)
	local versions = {}
	local sizes = {}
	local md5 = {}
	local md5_size = {}
	local file_sizes = manifest.file_sizes or {}
	local file_md5 = manifest.md5_chunk_size and manifest.file_md5_size and manifest.file_md5 or {}
	local file_md5_size = manifest.file_md5_size or {}
	for file_name, info in pairs(manifest.file_versions) do
		if type(info) == "table" then
			versions[file_name] = info.version
			sizes[file_name] = info.size or file_sizes[file_name] or 0
		else
			versions[file_name] = info
			-- legacy manifests have no file_sizes, size stays unknown
			sizes[file_name] = file_sizes[file_name] or 0
		end
		md5[file_name] = file_md5[file_name]
		md5_size[file_name] = file_md5_size[file_name]
	end

	local merged_into = {}
	local pack_set = {}
	for pack_name, members in pairs(manifest.merged_archives or {}) do
		pack_set[pack_name] = true
		for _, member in ipairs(members) do
			merged_into[member] = pack_name
		end
	end

	local dep_lists = {}
	local dep_set = {}
	for dep_name, collections in pairs(manifest.deps or {}) do
		dep_set[dep_name] = true
		for _, file_name in ipairs(collections) do
			if not dep_lists[file_name] then
				dep_lists[file_name] = {}
			end
			table.insert(dep_lists[file_name], dep_name)
		end
	end

	return {
		versions = versions,
		sizes = sizes,
		md5 = md5,
		md5_chunk_size = manifest.md5_chunk_size or 0,
		md5_size = md5_size,
		merged_into = merged_into,
		pack_set = pack_set,
		dep_lists = dep_lists,
		dep_set = dep_set,
		path = path,
		prefix = prefix,
	}
end

---@param side liveupdater.manifest_side
---@param archive_name string
---@return string
local function resolve_archive_name(side, archive_name)
	return side.merged_into[archive_name] or archive_name
end

---@param manifest_group liveupdater.manifest_group
---@return liveupdater.manifest_cache
local function build_manifest_cache(manifest_group)
	return {
		low = manifest_group.lowres and build_manifest_side(manifest_group.lowres, low_res_path, LOWRES_PREFIX) or nil,
		high = manifest_group.highres and build_manifest_side(manifest_group.highres, highres_path, HIGH_PREFIX) or nil,
	}
end

---@param side liveupdater.manifest_side?
---@param archive_name string
---@return string? version, string resolved_name
local function side_archive_version(side, archive_name)
	if not side then
		return nil, archive_name
	end
	local resolved = resolve_archive_name(side, archive_name)
	return side.versions[resolved], resolved
end

---@param file_name string
---@return string? low_version, string? high_version
local function get_actual_versions(file_name)
	assert(manifest_cache, "Liveupdater: manifest cache not built")
	local low_version = side_archive_version(manifest_cache.low, file_name)
	local high_version = side_archive_version(manifest_cache.high, file_name)
	return low_version or nil, high_version or nil
end

---@return liveupdater.manifest_side[]
local function active_manifest_sides()
	local sides = {}
	if not manifest_cache then
		return sides
	end
	if manifest_cache.low then
		sides[#sides + 1] = manifest_cache.low
	end
	if manifest_cache.high then
		sides[#sides + 1] = manifest_cache.high
	end
	return sides
end

---@param module liveupdater.module
---@return string[]
local function module_archive_names(module)
	local names = {}
	local seen = {}
	local function add(name)
		if name and not seen[name] then
			seen[name] = true
			names[#names + 1] = name
		end
	end
	for _, file_name in ipairs(module.files) do
		for _, side in ipairs(active_manifest_sides()) do
			local _, resolved = side_archive_version(side, file_name)
			add(resolved)
			for _, dep_name in ipairs(side.dep_lists[file_name] or {}) do
				local _, dep_resolved = side_archive_version(side, dep_name)
				add(dep_resolved)
			end
		end
	end
	return names
end

---@param archive_name string
---@return string?
local function get_saved_version(archive_name)
	local direct = M.saved_list[archive_name]
	if direct then
		return direct
	end
	for _, side in ipairs(active_manifest_sides()) do
		local pack = side.merged_into[archive_name]
		if pack and M.saved_list[pack] then
			return M.saved_list[pack]
		end
	end
	return nil
end

---@param archive_name string
---@return boolean
local function archive_awaits_high(archive_name)
	local low_version, high_version = get_actual_versions(archive_name)
	if not high_version or high_version == low_version then
		return false
	end
	return get_saved_version(archive_name) ~= high_version
end

---@param module liveupdater.module
---@return liveupdater.res_mode
local function get_res_mode(module)
	return module.res_mode or RES_HIGH_ONLY
end

---Which resolutions a module should be processed in, intersecting its res_mode
---with the resolutions active this session. When only one resolution is active
---res_mode is ignored: every module loads that single resolution.
---@param module liveupdater.module
---@return boolean supports_low, boolean supports_high
local function get_module_resolutions(module)
	if active_low and active_high then
		local res_mode = get_res_mode(module)
		return res_mode == RES_LOW_ONLY or res_mode == RES_BOTH, res_mode == RES_HIGH_ONLY or res_mode == RES_BOTH
	end
	return active_low, active_high
end

---Fetches the manifest of each active resolution and returns them as a group.
---Only the active sides are downloaded; a single-resolution session hits one URL.
---@param cb fun(success: boolean, result: liveupdater.manifest_group|string)
local function check_manifests(client_version, cb)
	---@type liveupdater.manifest_group
	local result = {}

	local function check_client_version(manifest, resolution)
		if client_version ~= nil and manifest.client_version ~= client_version then
			cb(
				false,
				string.format(
					"Client version mismatch in %s manifest: expected %s, got %s",
					resolution,
					client_version,
					tostring(manifest.client_version)
				)
			)
			return false
		end
		return true
	end

	---@param next_step fun()
	local function fetch_lowres(next_step)
		if not active_low then
			next_step()
			return
		end
		request_data(LOWRES_PREFIX, low_res_path, manifest_json, manifest_json, function(success, data)
			if not success then
				cb(false, "Unable to get low res manifest")
				return
			end
			local manifest = decode_json_data(data)
			if not manifest then
				cb(false, "Unable to parse low res manifest")
				return
			end
			if not check_client_version(manifest, "lowres") then
				return
			end
			result.lowres = manifest
			next_step()
		end)
	end

	---@param next_step fun()
	local function fetch_highres(next_step)
		if not active_high then
			next_step()
			return
		end
		request_data(HIGH_PREFIX, highres_path, manifest_json, manifest_json, function(success, data)
			if not success then
				cb(false, "Unable to get highres manifest")
				return
			end
			local manifest = decode_json_data(data)
			if not manifest then
				cb(false, "Unable to parse highres manifest")
				return
			end
			if not check_client_version(manifest, "highres") then
				return
			end
			result.highres = manifest
			next_step()
		end)
	end

	fetch_lowres(function()
		fetch_highres(function()
			cb(true, result)
		end)
	end)
end

---@param file_name string
---@return boolean
local function remove_mount_and_local_file(file_name)
	if not remove_engine_mount(file_name) then
		return false
	end
	local result, err = remove_local_file(file_name)
	if not result then
		log.error("Unable to remove file %s: %s", file_name, err)
		return false
	end
	set_saved_file_version(file_name, nil)
	return true
end

---@class liveupdater.cache_check_entry
---@field file_name string
---@field version string
---@field md5 string
---@field chunk_size integer
---@field size integer

---@type liveupdater.cache_check_entry[]
local cache_check_list = {}
local cache_check_generation = 0
local cache_check_started = false
local CACHE_CHECK_START_DELAY = 10
local CACHE_CHECK_IDLE_DELAY = 1

---@param filename string
---@return boolean
local function drop_for_failed_md5(filename)
	local state = cache_md5[filename]
	if not state or not state.failed then
		return false
	end
	if state.md5 ~= get_expected_md5(filename, M.saved_list[filename]) then
		cache_md5[filename] = nil
		return false
	end
	state.failed = nil
	state.retried = true
	return true
end

---@param filename string
local function queue_cache_check(filename)
	if not verify_hashes then
		return
	end
	local version = M.saved_list[filename]
	local expected, chunk_size, size = get_expected_md5(filename, version)
	if not version or not expected or not chunk_size or chunk_size <= 0 or not size then
		return
	end
	local state = cache_md5[filename]
	if state and state.ok and state.md5 == expected then
		return
	end
	table.insert(
		cache_check_list,
		{ file_name = filename, version = version, md5 = expected, chunk_size = chunk_size, size = size }
	)
end

---@param entry liveupdater.cache_check_entry
---@param actual string
local function finish_cache_check(entry, actual)
	if M.saved_list[entry.file_name] ~= entry.version then
		return
	end
	if md5_accepted(entry.md5, actual) then
		cache_md5[entry.file_name] = { md5 = entry.md5, ok = true }
		return
	end
	local state = cache_md5[entry.file_name]
	if state and state.retried and state.md5 == entry.md5 then
		log.error(
			"Cached archive md5 mismatch after redownload %s: expected=%s got=%s",
			entry.file_name,
			entry.md5,
			actual
		)
		cache_md5[entry.file_name] = { md5 = entry.md5, ok = true }
		return
	end
	log.warn(
		"Cached archive md5 mismatch %s: expected=%s got=%s, redownload on next start",
		entry.file_name,
		entry.md5,
		actual
	)
	cache_md5[entry.file_name] = { md5 = entry.md5, failed = true }
end

local function run_cache_check()
	local generation = cache_check_generation
	local index = 1
	local total_bytes = 0
	local total_ms = 0
	---@type liveupdater.cache_check_entry?
	local entry = nil
	---@type file?
	local file = nil
	---@type string[]
	local parts = {}
	local left = 0

	local function close_file()
		if file then
			file:close()
			file = nil
		end
		entry = nil
	end

	local function step()
		if generation ~= cache_check_generation then
			close_file()
			return
		end
		if next(in_flight) or mount_queue[1] or mount_busy then
			close_file()
			timer.delay(CACHE_CHECK_IDLE_DELAY, false, step)
			return
		end
		local deadline = socket.gettime() + POST_BUDGET_SEC
		repeat
			if not entry then
				entry = cache_check_list[index]
				if not entry then
					save_local_files_list()
					M.bench_mark(
						"cache_md5",
						string.format("files=%d bytes=%d ms=%.1f", index - 1, total_bytes, total_ms)
					)
					cache_check_list = {}
					return
				end
				parts = {}
				left = entry.size
				if M.saved_list[entry.file_name] == entry.version then
					file = io.open(get_local_path(entry.file_name), "rb")
				end
				if not file then
					entry = nil
					index = index + 1
				end
			end
			if entry and file then
				local t0 = socket.gettime()
				local chunk = left > 0 and file:read(math.min(entry.chunk_size, left))
				if chunk then
					left = left - #chunk
					parts[#parts + 1] = hashmd5(chunk)
					total_bytes = total_bytes + #chunk
				else
					local done = entry
					close_file()
					finish_cache_check(done, hashmd5(table.concat(parts)))
					index = index + 1
				end
				total_ms = total_ms + (socket.gettime() - t0) * 1000
			end
		until socket.gettime() >= deadline
		timer.delay(0, false, step)
	end

	step()
end

local function start_cache_check()
	if cache_check_started or not cache_check_list[1] then
		return
	end
	cache_check_started = true
	local generation = cache_check_generation
	timer.delay(CACHE_CHECK_START_DELAY, false, function()
		if generation == cache_check_generation then
			run_cache_check()
		end
	end)
end

---@param filename string
---@return integer?
local function local_file_size(filename)
	local file = io.open(get_local_path(filename), "rb")
	if not file then
		return nil
	end
	local size = file:seek("end")
	file:close()
	return size
end

local function restore_staged_high()
	if staged_restored then
		return
	end
	staged_restored = true
	if not next(staged_versions) then
		return
	end
	for file_name, version in pairs(staged_versions) do
		local pending_name = file_name .. REMOUNT_PENDING_SUFFIX
		local _, high_version = get_actual_versions(file_name)
		local expected_size = manifest_cache and manifest_cache.high and manifest_cache.high.sizes[file_name] or 0
		local restored = false
		if version == high_version and file_exists(pending_name) and local_file_size(pending_name) == expected_size then
			remove_engine_mount(pending_name)
			if remove_engine_mount(file_name) then
				local moved, err = move_local_file(pending_name, file_name)
				if moved then
					M.saved_list[file_name] = version
					restored = true
					M.bench_mark("staged_restored", "name=" .. file_name)
				else
					log.error("Unable to restore staged high %s: %s", file_name, err)
				end
			end
		end
		if not restored then
			remove_engine_mount(pending_name)
			remove_local_file(pending_name)
		end
		staged_versions[file_name] = nil
	end
	save_local_files_list()
end

---Removes stale local files and mounts the up-to-date ones.
---@param callback fun(success: boolean, error_text: string?)
local function sync_local_files(callback)
	rebuild_mounts_index()
	restore_staged_high()
	cache_check_generation = cache_check_generation + 1
	cache_check_started = false
	cache_check_list = {}
	local to_mount = {}
	local stale = {}
	local missing = {}
	for filename, saved_version in pairs(M.saved_list) do
		local low_version, high_version = get_actual_versions(filename)
		if saved_version ~= low_version and saved_version ~= high_version then
			table.insert(stale, filename)
		elseif drop_for_failed_md5(filename) then
			table.insert(stale, filename)
		elseif file_exists(filename) then
			table.insert(to_mount, filename)
		else
			table.insert(missing, filename)
		end
	end

	for _, filename in ipairs(stale) do
		if not remove_mount_and_local_file(filename) then
			log.error("Unable to remove file from storage %s", filename)
			callback(false, "Unable to remove file " .. filename)
			return
		end
	end

	for _, filename in ipairs(missing) do
		if not remove_mount_and_local_file(filename) then
			log.warn("Unable to cleanup invalid saved file %s", filename)
		end
	end

	local function mount_next(index, files)
		if index > #files then
			callback(true)
			return
		end
		local filename = files[index]
		mount_resource(filename, function(success)
			if not success then
				log.warn("Unable to mount cached archive %s, it will be downloaded again", filename)
				remove_local_file(filename)
				set_saved_file_version(filename, nil)
			else
				queue_cache_check(filename)
			end
			mount_next(index + 1, files)
		end)
	end

	for filename in pairs(cache_md5) do
		local low_version, high_version = get_actual_versions(filename)
		if not low_version and not high_version then
			cache_md5[filename] = nil
		end
	end

	mount_next(1, to_mount)
end

local function sort_download_queue()
	table.sort(download_files_queue, function(a, b)
		if a.priority == b.priority then
			return a.order < b.order
		end
		return a.priority < b.priority
	end)
end

---@param modules table<string, liveupdater.module>
---@param force boolean?
---@return boolean
local function enqueue_modules(modules, force)
	assert(manifest_cache, "Liveupdater: manifest cache not built")
	local low = manifest_cache.low
	local high = manifest_cache.high
	local changed = false

	---@param claims table<string, {priority: number, class: integer}>
	---@return number priority, integer class
	local function winning_claim(claims)
		local best_priority = nil
		local best_class = nil
		for _, claim in pairs(claims) do
			local tighter_priority = best_priority == nil or claim.priority < best_priority
			local same_priority = best_priority ~= nil and claim.priority == best_priority
			local tighter_class = same_priority and claim.class < best_class
			if tighter_priority or tighter_class then
				best_priority = claim.priority
				best_class = claim.class
			end
		end
		return best_priority or 0, best_class or M.DOWNLOAD_CLASS.OTHER
	end

	---@param item liveupdater.queue_item
	---@param reason string
	---@param requester_id string
	---@param download_class integer M.DOWNLOAD_CLASS.*
	local function add_to_queue(item, reason, requester_id, download_class)
		local key = item.file_name .. ":" .. item.version
		local claims = queue_requesters[key]
		if not claims then
			claims = {}
			queue_requesters[key] = claims
		end
		claims[requester_id .. ":" .. item.resolution] = { priority = item.priority, class = download_class }
		local effective, class = winning_claim(claims)
		item.priority = effective
		item.download_class = class

		local existing = download_queue_index[key]
		if existing then
			local same = existing.priority == effective and existing.download_class == class
			existing.priority = effective
			existing.download_class = class
			if in_flight[key] or same then
				return
			end
			existing.reason = reason
			changed = true
			return
		end
		local item_bytes = session_bytes[item.resolution]
		item_bytes.total = item_bytes.total + item.size
		download_queue_order = download_queue_order + 1
		item.order = download_queue_order
		item.reason = reason
		download_queue_index[key] = item
		table.insert(download_files_queue, item)
		changed = true
	end

	---@param side liveupdater.manifest_side
	---@param file_name string
	---@param base_priority number
	---@param resolution liveupdater.resolution
	---@param requester_id string
	---@param download_class integer M.DOWNLOAD_CLASS.*
	---@param satisfied_priority number?
	local function add_dependencies(
		side,
		file_name,
		base_priority,
		resolution,
		requester_id,
		download_class,
		satisfied_priority
	)
		for _, dep_name in ipairs(side.dep_lists[file_name] or {}) do
			local resolved = resolve_archive_name(side, dep_name)
			local saved_dep_version = get_saved_version(dep_name)
			local dep_version_low = side_archive_version(low, dep_name)
			local dep_version_high = side_archive_version(high, dep_name)
			local skip_dep = false
			if
				saved_dep_version
				and resolution == M.RESOLUTION.LOW
				and (saved_dep_version == dep_version_low or saved_dep_version == dep_version_high)
			then
				skip_dep = true
			end
			if saved_dep_version and resolution == M.RESOLUTION.HIGH and saved_dep_version == dep_version_high then
				skip_dep = true
			end
			if
				resolution == M.RESOLUTION.HIGH
				and dep_version_high
				and staged_versions[resolved] == dep_version_high
			then
				skip_dep = true
			end
			if not skip_dep then
				local version = resolution == M.RESOLUTION.HIGH and dep_version_high or dep_version_low
				local dep_priority = base_priority
				local dep_class = download_class
				if satisfied_priority and saved_dep_version and saved_dep_version == dep_version_low then
					dep_priority = satisfied_priority
					dep_class = M.DOWNLOAD_CLASS.OTHER
				end
				if version then
					add_to_queue({
						file_name = resolved,
						priority = dep_priority - DEPENDENCY_PRIORITY_OFFSET,
						version = version,
						size = side.sizes[resolved] or 0,
						module_name = COMMON_MODULE,
						path = side.path,
						prefix = side.prefix,
						resolution = resolution,
						remount = resolution == M.RESOLUTION.HIGH,
					}, string.format("dependency_of=%s", file_name), requester_id, dep_class)
				end
			end
		end
	end

	for module_name, module in pairs(modules) do
		local available = true
		if not force and module.by_request then
			available = false
		elseif not is_module_available(module_name) then
			available = false
		end

		if available then
			module.processed = true
			local low_priority = module.focus_low
			if low_priority == nil then
				low_priority = module.priority
			end
			local high_priority = module.focus_high
			if high_priority == nil then
				high_priority = module.priority + HIGH_PRIORITY_OFFSET
			end
			local download_class = M.DOWNLOAD_CLASS.BOOT
			local blocking = M.PRIORITY[M.PRIORITY_IDS.BLOCKING]
			if module.focus_low ~= nil and module.focus_high ~= nil then
				if module.focus_low == blocking.low and module.focus_high == blocking.high then
					download_class = M.DOWNLOAD_CLASS.BLOCKING
				else
					download_class = M.DOWNLOAD_CLASS.OTHER
				end
			end
			local module_low, module_high = get_module_resolutions(module)
			local satisfied_dep_priority = nil
			if module.focus_low ~= nil and module_high and not module_low then
				satisfied_dep_priority = high_priority
				high_priority = low_priority
			end
			for _, file_name in ipairs(module.files) do
				local saved_version = get_saved_version(file_name)
				local low_version = side_archive_version(low, file_name)
				local high_version = side_archive_version(high, file_name)
				local low_resolved = low and resolve_archive_name(low, file_name) or file_name
				local high_resolved = high and resolve_archive_name(high, file_name) or file_name
				local supports_low, supports_high = get_module_resolutions(module)

				local needs_update = not saved_version
					or (saved_version ~= low_version and saved_version ~= high_version)

				if supports_low then
					add_dependencies(low, file_name, low_priority, M.RESOLUTION.LOW, module_name, download_class)
					if low_version then
						local low_key = low_resolved .. ":" .. low_version
						if needs_update or download_queue_index[low_key] then
							add_to_queue({
								file_name = low_resolved,
								priority = low_priority,
								version = low_version,
								size = low.sizes[low_resolved] or 0,
								module_name = module_name,
								path = low.path,
								prefix = low.prefix,
								resolution = M.RESOLUTION.LOW,
							}, "needs_low", module_name, download_class)
						end
					end
				end

				if supports_high then
					add_dependencies(
						high,
						file_name,
						high_priority,
						M.RESOLUTION.HIGH,
						module_name,
						download_class,
						satisfied_dep_priority
					)
					local high_staged = high_version ~= nil and staged_versions[high_resolved] == high_version
					if high_version and not high_staged and (not supports_low or high_version ~= low_version) then
						local high_key = high_resolved .. ":" .. high_version
						if needs_update or download_queue_index[high_key] then
							add_to_queue({
								file_name = high_resolved,
								priority = high_priority,
								version = high_version,
								size = high.sizes[high_resolved] or 0,
								module_name = module_name,
								path = high.path,
								prefix = high.prefix,
								resolution = M.RESOLUTION.HIGH,
								remount = true,
							}, "needs_high", module_name, download_class)
						end
					end
				end
			end
		end
	end

	return changed
end

---@param item liveupdater.queue_item
---@return string
local function queue_item_key(item)
	return item.file_name .. ":" .. item.version
end

---@param item liveupdater.queue_item
---@return integer M.DOWNLOAD_CLASS.*
local function item_download_class(item)
	return item.download_class or M.DOWNLOAD_CLASS.OTHER
end

---@return integer boot, integer blocking, integer other, integer total
local function count_in_flight_classes()
	local boot, blocking, other = 0, 0, 0
	for _, state in pairs(in_flight) do
		if state.phase == M.IN_FLIGHT_PHASE.DOWNLOADING then
			local class = item_download_class(state.item)
			if class == M.DOWNLOAD_CLASS.BOOT then
				boot = boot + 1
			elseif class == M.DOWNLOAD_CLASS.BLOCKING then
				blocking = blocking + 1
			else
				other = other + 1
			end
		end
	end
	return boot, blocking, other, boot + blocking + other
end

---@return integer
local function mount_queue_pending_bodies()
	return #mount_queue
end

---@param item liveupdater.queue_item
---@return boolean
local function archive_version_conflict(item)
	local file_name = item.file_name
	for _, state in pairs(in_flight) do
		local other = state.item
		if other.file_name == file_name and other.version ~= item.version then
			return true
		end
	end
	for _, job in ipairs(mount_queue) do
		local other = job.load_info
		if other.file_name == file_name and other.version ~= item.version then
			return true
		end
	end
	if mounting_files[file_name] then
		return true
	end
	return false
end

---@param item liveupdater.queue_item
---@return boolean
local function can_start_download(item)
	if archive_version_conflict(item) then
		return false
	end
	if mount_queue_pending_bodies() >= DISK_POST_QUEUE_LIMIT then
		return false
	end
	local boot_n, blocking_n, other_n, total = count_in_flight_classes()
	if parallel_downloads <= 1 then
		return total == 0
	end
	local class = item_download_class(item)
	if class == M.DOWNLOAD_CLASS.BLOCKING then
		return blocking_n == 0
	end
	if class == M.DOWNLOAD_CLASS.BOOT then
		if other_n > 0 then
			return false
		end
		return boot_n < parallel_downloads
	end
	return total == 0
end

---@param modules table<string, liveupdater.module>
local function check_modules_integrity(modules)
	assert(manifest_cache, "Liveupdater: manifest cache not built")
	-- highres is the superset when both are active; fall back to lowres when it is
	-- the only active resolution
	local reference_side = manifest_cache.high or manifest_cache.low
	assert(reference_side, "Liveupdater: no active manifest side")

	local all_module_files = {}
	for _, module in pairs(modules) do
		for _, file_name in ipairs(module.files) do
			all_module_files[file_name] = true
		end
	end

	for file_name, _ in pairs(all_module_files) do
		local resolved = resolve_archive_name(reference_side, file_name)
		if not reference_side.versions[resolved] then
			local error_text =
				string.format("The module specifies a file that is not listed in the manifest. Module: %s", file_name)
			log.warn(error_text)
			downloadable_missing_files[file_name] = error_text
		end
	end

	for file_name, _ in pairs(reference_side.versions) do
		if
			all_module_files[file_name] == nil
			and not reference_side.dep_set[file_name]
			and not reference_side.pack_set[file_name]
		then
			local error_text =
				string.format("The manifest lists a file that is not present in the modules. File: %s", file_name)
			log.warn(error_text)
			downloadable_missing_files[file_name] = error_text
		end
	end
end

local try_start_load_resources
local pump_mount_queue
local schedule_mount_pump

local function finish_queue_item(load_info)
	local key = queue_item_key(load_info)
	in_flight[key] = nil
	download_queue_index[key] = nil
	queue_requesters[key] = nil
	for index, value in ipairs(download_files_queue) do
		if value.file_name == load_info.file_name and value.version == load_info.version then
			table.remove(download_files_queue, index)
			break
		end
	end
end

local function maybe_all_loaded()
	if next(download_files_queue) or next(in_flight) or mount_queue[1] or mount_busy then
		return
	end
	files_load_completed = true
	notify_event_listeners(M.MSG_ALL_LOADED)
	log.debug("All global files loaded")
	M.bench_mark("t_all_mounted")
	start_cache_check()
end

---@param file_name string
---@param module_name string
local function announce_module_loaded(file_name, module_name)
	if module_name == COMMON_MODULE then
		return
	end
	if not M.is_module_loaded(module_name) then
		modules_announced_loaded[module_name] = nil
		return
	end
	if modules_announced_loaded[module_name] then
		return
	end
	modules_announced_loaded[module_name] = true
	notify_event_listeners(M.MSG_MODULE_LOADED, {
		file_name = file_name,
		module_name = module_name,
	})
end

---@param file_name string
---@param module_name string
local function announce_loaded_after_mount(file_name, module_name)
	if module_name ~= COMMON_MODULE then
		announce_module_loaded(file_name, module_name)
		return
	end
	for name in pairs(M.modules) do
		announce_module_loaded(file_name, name)
	end
end

---@param load_info liveupdater.queue_item
local function on_mount_success(load_info)
	update_file_progress(load_info.prefix, load_info.file_name, 1, 1)
	mounting_files[load_info.file_name] = nil
	set_saved_file_version(load_info.file_name, load_info.version)
	local res_bytes = session_bytes[load_info.resolution]
	res_bytes.downloaded = res_bytes.downloaded + load_info.size
	finish_queue_item(load_info)

	notify_event_listeners(M.MSG_FILE_LOADED, {
		file_name = load_info.file_name,
		module_name = load_info.module_name,
	})
	announce_loaded_after_mount(load_info.file_name, load_info.module_name)

	schedule_mount_pump()
	try_start_load_resources()
	maybe_all_loaded()
end

---@param load_info liveupdater.queue_item
local function schedule_mount_retry(load_info)
	log.error("Unable to mount file %s", load_info.file_name)
	load_info.mount_attempts = (load_info.mount_attempts or 0) + 1
	if load_info.mount_attempts >= max_attempts then
		load_info.mount_attempts = 0
		mounting_files[load_info.file_name] = nil
		module_loaded_memo = {}
		remove_local_file(load_info.file_name)
		in_flight[queue_item_key(load_info)] = nil
		timer.delay(RETRY_DELAY, false, try_start_load_resources)
		schedule_mount_pump()
		return
	end
	timer.delay(RETRY_DELAY, false, function()
		table.insert(mount_queue, 1, { load_info = load_info })
		schedule_mount_pump()
	end)
end

---@param load_info liveupdater.queue_item
local function mount_one(load_info)
	local key = queue_item_key(load_info)
	local state = in_flight[key]
	if state then
		state.phase = M.IN_FLIGHT_PHASE.MOUNTING
		state.bytes = 0
	end
	mount_busy = true
	local t0 = socket.gettime()
	mount_resource(load_info.file_name, function(success)
		log_timing("mount", load_info.file_name, (socket.gettime() - t0) * 1000)
		mount_busy = false
		if success then
			on_mount_success(load_info)
		else
			schedule_mount_retry(load_info)
		end
	end)
end

---@param load_info liveupdater.queue_item
local function finish_staged_download(load_info)
	mounting_files[load_info.file_name] = nil
	remount_pending[load_info.file_name] = nil
	finish_queue_item(load_info)
	notify_event_listeners(M.MSG_FILE_LOADED, {
		file_name = load_info.file_name,
		module_name = load_info.module_name,
	})
	schedule_mount_pump()
	try_start_load_resources()
	maybe_all_loaded()
end

---@param load_info liveupdater.queue_item
---@param dl_name string
local function stage_high_below(load_info, dl_name)
	local file_name = load_info.file_name
	if staged_high[file_name] then
		remove_local_file(dl_name)
		finish_staged_download(load_info)
		return
	end

	local pending_name = file_name .. REMOUNT_PENDING_SUFFIX
	local low_mount = mounts_by_hash[hash(file_name)]
	local below = low_mount and (low_mount.priority - 1) or -1

	mounting_files[file_name] = true
	remount_pending[file_name] = pending_name
	module_loaded_memo = {}

	local t_save = socket.gettime()
	local saved, err = move_local_file(dl_name, pending_name)
	log_timing("save", pending_name, (socket.gettime() - t_save) * 1000)
	if not saved then
		log.error("Unable to stage high %s: %s", pending_name, err)
		mounting_files[file_name] = nil
		remount_pending[file_name] = nil
		in_flight[queue_item_key(load_info)] = nil
		timer.delay(RETRY_DELAY, false, try_start_load_resources)
		schedule_mount_pump()
		return
	end

	local function remember(mounted)
		staged_high[file_name] = {
			load_info = load_info,
			pending_name = pending_name,
			mounted = mounted,
		}
		staged_versions[file_name] = load_info.version
		save_local_files_list()
		log.debug("Staged high under pinned low %s", file_name)
		finish_staged_download(load_info)
	end

	if below < 0 then
		remember(false)
		return
	end

	mount_busy = true
	local pending_uri = zip_mount_uri(pending_name)
	if is_mount_exist(pending_name) then
		remove_engine_mount(pending_name)
	end
	local t_mount = socket.gettime()
	liveupdate.add_mount(pending_name, pending_uri, below, function(_, _, _, result)
		log_timing("mount", pending_name, (socket.gettime() - t_mount) * 1000)
		mount_busy = false
		if result ~= liveupdate.LIVEUPDATE_OK then
			log.error("Unable to mount staged high %s", pending_name)
			remember(false)
			return
		end
		register_mount(pending_name, pending_uri, below)
		remember(true)
	end)
end

local pump_promote
local promote_failed = false

---@param file_name string
local function fail_promote(file_name)
	staged_high[file_name] = nil
	staged_versions[file_name] = nil
	promote_failed = true
	mount_busy = false
	pump_promote()
end

---@param staged liveupdater.staged_high
---@param cb fun()
local function promote_one(staged, cb)
	local load_info = staged.load_info
	local file_name = load_info.file_name
	local pending_name = staged.pending_name
	staged.attempts = (staged.attempts or 0) + 1
	if staged.attempts > 2 then
		log.error("Giving up high promote for %s", file_name)
		fail_promote(file_name)
		return
	end
	if not file_exists(pending_name) then
		log.error("Staged high missing on disk %s", pending_name)
		fail_promote(file_name)
		return
	end

	mount_busy = true
	if staged.mounted then
		remove_engine_mount(pending_name)
	end
	remove_engine_mount(file_name)

	local t_save = socket.gettime()
	local saved, err = move_local_file(pending_name, file_name)
	log_timing("save", file_name, (socket.gettime() - t_save) * 1000)
	if not saved then
		log.error("Unable to promote high %s: %s", file_name, err)
		mount_busy = false
		staged.mounted = false
		timer.delay(RETRY_DELAY, false, function()
			promote_one(staged, cb)
		end)
		return
	end

	local function promoted()
		staged_versions[file_name] = nil
		set_saved_file_version(file_name, load_info.version)
		staged_high[file_name] = nil
		notify_event_listeners(M.MSG_FILE_LOADED, {
			file_name = file_name,
			module_name = load_info.module_name,
		})
		cb()
	end

	local final_uri = zip_mount_uri(file_name)
	local priority = get_next_priority()
	liveupdate.add_mount(file_name, final_uri, priority, function(_, _, _, result)
		if result == liveupdate.LIVEUPDATE_OK then
			register_mount(file_name, final_uri, priority)
			mount_busy = false
			promoted()
			return
		end
		log.error("Unable to mount promoted high %s", file_name)
		mount_resource(file_name, function(ok)
			mount_busy = false
			if ok then
				promoted()
			else
				fail_promote(file_name)
			end
		end)
	end)
end

local promote_waiting = false

pump_promote = function()
	if mount_busy then
		if not promote_waiting then
			promote_waiting = true
			timer.delay(0, false, function()
				promote_waiting = false
				pump_promote()
			end)
		end
		return
	end
	local job = table.remove(promote_jobs, 1)
	if not job then
		local cb = promote_on_done
		local ok = not promote_failed
		promote_on_done = nil
		promote_failed = false
		if cb then
			cb(ok)
		end
		schedule_mount_pump()
		try_start_load_resources()
		return
	end
	promote_one(job, pump_promote)
end

---@param load_info liveupdater.queue_item
---@return boolean
local function is_obsolete_low_write(load_info)
	if load_info.resolution ~= M.RESOLUTION.LOW then
		return false
	end
	local file_name = load_info.file_name
	local _, high_version = get_actual_versions(file_name)
	if high_version and get_saved_version(file_name) == high_version then
		return true
	end
	if staged_high[file_name] or remount_pending[file_name] then
		return true
	end
	for _, state in pairs(in_flight) do
		local other = state.item
		if other.file_name == file_name and other.resolution == M.RESOLUTION.HIGH then
			return true
		end
	end
	for _, job in ipairs(mount_queue) do
		local other = job.load_info
		if other.file_name == file_name and other.resolution == M.RESOLUTION.HIGH then
			return true
		end
	end
	return false
end

---@param load_info liveupdater.queue_item
---@param dl_name string
---@return boolean
local function download_size_matches(load_info, dl_name)
	local expected = load_info.size
	if not expected or expected <= 0 then
		return true
	end
	local file = io.open(get_local_path(dl_name), "rb")
	if not file then
		return false
	end
	local actual = file:seek("end")
	file:close()
	if actual ~= expected then
		log.warn("Download size mismatch for %s: expected=%d got=%s", load_info.file_name, expected, tostring(actual))
		return false
	end
	return true
end

---@param job liveupdater.mount_job
local function fail_verify_and_redownload(job)
	local load_info = job.load_info
	local key = queue_item_key(load_info)
	if job.dl_name then
		remove_local_file(job.dl_name)
	end
	load_info.verify_attempts = (load_info.verify_attempts or 0) + 1
	in_flight[key] = nil
	if load_info.verify_attempts < max_attempts then
		timer.delay(0, false, try_start_load_resources)
	else
		local backoff = math.min(RETRY_DELAY * 2 ^ (load_info.verify_attempts - max_attempts), MAX_RETRY_DELAY)
		load_info.retry_at = socket.gettime() + backoff
		notify_event_listeners(M.MSG_NETWORK_ERROR)
		timer.delay(backoff, false, try_start_load_resources)
	end
	schedule_mount_pump()
end

pump_mount_queue = function()
	if mount_busy or not mount_queue[1] then
		return
	end
	local job = table.remove(mount_queue, 1)
	local load_info = job.load_info
	local key = queue_item_key(load_info)
	local dl_name = job.dl_name
	if dl_name and is_obsolete_low_write(load_info) then
		remove_local_file(dl_name)
		finish_queue_item(load_info)
		schedule_mount_pump()
		try_start_load_resources()
		maybe_all_loaded()
		return
	end

	if dl_name and not job.size_ok then
		if not download_size_matches(load_info, dl_name) then
			fail_verify_and_redownload(job)
			return
		end
		job.size_ok = true
	end

	if dl_name then
		if load_info.remount and is_mount_exist(load_info.file_name) and pinned_archives[load_info.file_name] then
			stage_high_below(load_info, dl_name)
			return
		end
		if load_info.remount then
			mounting_files[load_info.file_name] = true
			module_loaded_memo = {}
			-- mount_resource skips an existing mount with the same uri, so the low copy has to go first
			remove_engine_mount(load_info.file_name)
			remove_local_file(load_info.file_name)
		end
		local t_save = socket.gettime()
		local result, err = move_local_file(dl_name, load_info.file_name)
		log_timing("save", load_info.file_name, (socket.gettime() - t_save) * 1000)
		if not result then
			log.error("Unable to place file %s: %s", load_info.file_name, err)
			mounting_files[load_info.file_name] = nil
			in_flight[key] = nil
			timer.delay(RETRY_DELAY, false, try_start_load_resources)
			schedule_mount_pump()
			return
		end
	end
	mount_one(load_info)
end

schedule_mount_pump = function()
	if mount_pump_scheduled then
		return
	end
	mount_pump_scheduled = true
	timer.delay(0, false, function()
		mount_pump_scheduled = false
		pump_mount_queue()
	end)
end

---@param success boolean
---@param dl_name string?
---@param load_info liveupdater.queue_item
local function handle_load_info_callback(success, dl_name, load_info)
	local key = queue_item_key(load_info)
	if success then
		if is_obsolete_low_write(load_info) then
			if dl_name then
				remove_local_file(dl_name)
			end
			finish_queue_item(load_info)
			try_start_load_resources()
			maybe_all_loaded()
			return
		end
		table.insert(mount_queue, { load_info = load_info, dl_name = dl_name })
		local state = in_flight[key]
		if state then
			state.phase = M.IN_FLIGHT_PHASE.MOUNTING
			state.bytes = 0
		end
		schedule_mount_pump()
		try_start_load_resources()
	else
		notify_event_listeners(M.MSG_NETWORK_ERROR)
		in_flight[key] = nil
		timer.delay(RETRY_DELAY, false, try_start_load_resources)
	end
end

---@param load_info liveupdater.queue_item
local function start_download(load_info)
	local key = queue_item_key(load_info)
	in_flight[key] = {
		item = load_info,
		bytes = 0,
		phase = M.IN_FLIGHT_PHASE.DOWNLOADING,
	}

	local callback = function(success, dl_name)
		handle_load_info_callback(success, dl_name, load_info)
	end

	---@param bytes_received number
	---@param bytes_total number
	local progress_callback = function(bytes_received, bytes_total)
		local state = in_flight[key]
		if state and load_info.size > 0 then
			state.bytes = math.min(bytes_received, load_info.size)
		end
		if bytes_total > 0 then
			update_file_progress(
				load_info.prefix,
				load_info.file_name,
				math.min(bytes_received / bytes_total, 0.999),
				1
			)
		end
	end

	request_archive_file(
		load_info.prefix,
		load_info.path,
		build_archive_filename(load_info.file_name, load_info.version),
		load_info.file_name,
		callback,
		nil,
		load_info.version,
		progress_callback
	)
end

try_start_load_resources = function()
	local started = false
	local now = socket.gettime()
	for _, load_info in ipairs(download_files_queue) do
		local key = queue_item_key(load_info)
		local waiting_retry = load_info.retry_at and load_info.retry_at > now
		if not in_flight[key] and not waiting_retry and can_start_download(load_info) then
			start_download(load_info)
			started = true
			if parallel_downloads <= 1 then
				return
			end
		end
	end
	if
		not started
		and not next(download_files_queue)
		and not next(in_flight)
		and not mount_queue[1]
		and not mount_busy
	then
		files_load_completed = true
		start_cache_check()
	end
end

---@param manifest_group liveupdater.manifest_group
---@param cb fun(success: boolean, error_text: string?)
local function initialize_from_manifests(manifest_group, cb)
	manifest_cache = build_manifest_cache(manifest_group)
	module_loaded_memo = {}
	modules_announced_loaded = {}

	sync_local_files(function(success, error_text)
		if not success then
			cb(false, error_text)
			return
		end

		download_files_queue = {}
		download_queue_index = {}
		download_queue_order = 0
		queue_requesters = {}
		in_flight = {}
		mount_queue = {}
		mount_busy = false
		mounting_files = {}
		remount_pending = {}
		pinned_archives = {}
		pinned_modules = {}
		staged_high = {}
		promote_jobs = {}
		promote_on_done = nil
		session_bytes.low = { total = 0, downloaded = 0 }
		session_bytes.high = { total = 0, downloaded = 0 }
		if enqueue_modules(M.modules) then
			sort_download_queue()
		end

		check_modules_integrity(M.modules)

		for filename, _ in pairs(M.saved_list) do
			update_file_progress(FROMLOCAL_PREFIX, filename, 1, 1)
		end

		try_start_load_resources()

		M.initialized = true
		notify_event_listeners(M.MSG_INITIALIZED)
		cb(true)
	end)
end

---@param module_name string
---@return boolean
function M.has_module(module_name)
	assert(M.initialized, "Liveupdater: not initialized")
	assert(M.modules, "Liveupdater: modules not initialized")
	return M.modules[module_name] ~= nil
end

---@param archive_name string
---@return boolean
local function archive_file_exists(archive_name)
	if file_exists(archive_name) then
		return true
	end
	local pending = remount_pending[archive_name]
	if pending and file_exists(pending) then
		return true
	end
	for _, side in ipairs(active_manifest_sides()) do
		local pack = side.merged_into[archive_name]
		if pack and file_exists(pack) then
			return true
		end
	end
	return false
end

---@param archive_name string
---@return boolean
local function archive_is_mounting(archive_name)
	if mounting_files[archive_name] then
		return true
	end
	for _, side in ipairs(active_manifest_sides()) do
		local pack = side.merged_into[archive_name]
		if pack and mounting_files[pack] then
			return true
		end
	end
	return false
end

---@param module liveupdater.module
---@return boolean
local function compute_module_loaded(module)
	local cache = assert(manifest_cache, "Liveupdater: manifest cache not built")
	local supports_low, supports_high = get_module_resolutions(module)

	for _, file_name in ipairs(module.files) do
		if archive_is_mounting(file_name) and not remount_pending[file_name] then
			return false
		end
		local saved_version = get_saved_version(file_name)
		if not saved_version or not archive_file_exists(file_name) then
			return false
		end

		local low_version, high_version = get_actual_versions(file_name)
		local matches_low = supports_low and saved_version == low_version
		local matches_high = supports_high and saved_version == high_version
		if not matches_low and not matches_high then
			return false
		end

		local side = matches_high and cache.high or cache.low
		for _, dep_name in ipairs(side.dep_lists[file_name] or {}) do
			if archive_is_mounting(dep_name) and not remount_pending[dep_name] then
				return false
			end
			local dep_saved_version = get_saved_version(dep_name)
			local dep_low_version, dep_high_version = get_actual_versions(dep_name)
			local dep_valid = dep_saved_version
				and (dep_saved_version == dep_high_version or dep_saved_version == dep_low_version)
			if not dep_valid then
				return false
			end
		end
	end
	return true
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

	local memo = module_loaded_memo[module_name]
	if memo ~= nil then
		return memo
	end

	local loaded = compute_module_loaded(module)
	module_loaded_memo[module_name] = loaded
	return loaded
end

---@param options liveupdater.init_options
---@param cb fun(success: boolean, error_text: string?)
---@param module_available_fn (fun(module_name: string): boolean)? predicate gating non by_request modules; defaults to always-available
function M.init(options, cb, module_available_fn)
	M.modules = copy_table(options.modules, true)
	if module_available_fn then
		is_module_available = module_available_fn
	end
	if not is_liveupdate_available() then
		M.initialized = true
		notify_event_listeners(M.MSG_INITIALIZED)
		cb(true)
		return
	end
	if
		options.client_version ~= nil
		and (type(options.client_version) ~= "string" or not options.client_version:match("%S"))
	then
		cb(false, "Liveupdater: client_version must be a non-empty string")
		return
	end
	assert(M.save_cache_key, "Liveupdater: save_cache_key not set; call M.set_save_cache_key before M.init")
	load_local_files_list()

	assert(options, "Liveupdater: options not provided")
	assert(
		options.lowres_server_path or options.hires_server_path,
		"Liveupdater: at least one of lowres_server_path / hires_server_path must be provided"
	)
	highres_path = options.hires_server_path
	low_res_path = options.lowres_server_path
	active_low = options.lowres_server_path ~= nil
	active_high = options.hires_server_path ~= nil

	if html5 then
		check_manifests(options.client_version, function(success, result)
			if success then
				---@cast result -string
				initialize_from_manifests(result, cb)
			else
				cb(false, result)
			end
		end)
	end
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
			local file_progress = file_info.progress or 0
			table.insert(module_info.files, {
				name = prefix_filename,
				progress = file_progress,
			})

			if file_progress == 100 then
				module_loaded = module_loaded + 1
			end
		else
			table.insert(module_info.files, {
				name = NOTLOAD_PREFIX .. file_name,
				progress = 0,
			})
		end
	end

	if module_loaded > 0 then
		module_info.progress = (module_loaded / module_total) * 100
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

---@param total_bytes integer
---@param downloaded_bytes integer
---@param pending_files integer
---@param forced_progress number?
---@return liveupdater.resolution_progress
local function build_progress(total_bytes, downloaded_bytes, pending_files, forced_progress)
	local progress = forced_progress
	if not progress then
		if pending_files == 0 then
			progress = 100
		elseif total_bytes > 0 then
			-- zero-size pending archives (manifest without file_sizes) must not
			-- let the value reach 100 while something is still downloading
			progress = math.min(99.9, (downloaded_bytes / total_bytes) * 100)
		else
			progress = 0
		end
	end
	return {
		total_bytes = total_bytes,
		downloaded_bytes = downloaded_bytes,
		pending_files = pending_files,
		progress = progress,
	}
end

---@param forced_progress number?
---@return liveupdater.download_progress
local function build_idle_progress(forced_progress)
	local result = build_progress(0, 0, 0, forced_progress) --[[@as liveupdater.download_progress]]
	result.low = build_progress(0, 0, 0, forced_progress)
	result.high = build_progress(0, 0, 0, forced_progress)
	result.gate = build_progress(0, 0, 0, forced_progress)
	result.downloading = {}
	return result
end

---Byte-based progress for a module or a set of modules, with a per-resolution
---breakdown in the low/high fields. Shared dependency archives are counted once
---per set; an archive identical in both manifests counts toward the resolution
---counted first (for RES_BOTH that is lowres — the pass that actually downloads
---it). Anchored to the download queue: an archive counts as downloaded when
---nothing for it is pending, so the result reaches 100 exactly when the set's
---downloads drain. For by_request modules that were not requested yet, the
---future queue content is predicted from the same rules enqueue_modules uses;
---that prediction can dip for a moment while a shared archive is remounted to
---its highres copy, it recovers on the next mount.
---@param module_names string|string[]
---@return liveupdater.download_progress
function M.get_modules_download_progress(module_names)
	if type(module_names) == "string" then
		module_names = { module_names }
	end

	if not is_liveupdate_available() then
		return build_idle_progress()
	end
	if not M.initialized or not manifest_cache then
		return build_idle_progress(0)
	end
	local cache = manifest_cache

	local counters = {
		low = { total = 0, downloaded = 0, pending = 0 },
		high = { total = 0, downloaded = 0, pending = 0 },
	}
	local gate = { total = 0, downloaded = 0, pending = 0 }
	local counted = {}
	local counted_gate = {}

	---@param counter {total: number, downloaded: number, pending: number}
	---@param key string
	---@param size number
	---@param done boolean
	local function add_to_counter(counter, key, size, done)
		counter.total = counter.total + size
		if done then
			counter.downloaded = counter.downloaded + size
			return
		end
		counter.pending = counter.pending + 1
		local state = in_flight[key]
		if state and state.phase == M.IN_FLIGHT_PHASE.DOWNLOADING and state.bytes > 0 then
			counter.downloaded = counter.downloaded + math.min(state.bytes, size)
		end
	end

	---@param archive_name string
	---@param side liveupdater.manifest_side
	---@param done boolean
	---@param gating boolean
	---@param gate_done boolean?
	local function count_archive(archive_name, side, done, gating, gate_done)
		local version = side.versions[archive_name]
		if not version then
			return
		end
		local key = archive_name .. ":" .. version
		local size = side.sizes[archive_name]
		if not counted[key] then
			counted[key] = true
			add_to_counter(side == cache.low and counters.low or counters.high, key, size, done)
		end
		if gating and not counted_gate[key] then
			counted_gate[key] = true
			if gate_done == nil then
				gate_done = done
			end
			local state = in_flight[key]
			if gate_done and not done and state and state.phase == M.IN_FLIGHT_PHASE.DOWNLOADING then
				gate_done = false
			end
			add_to_counter(gate, key, size, gate_done)
		end
	end

	---@param archive_name string
	---@param side liveupdater.manifest_side
	---@return boolean
	local function is_queued(archive_name, side)
		local resolved = resolve_archive_name(side, archive_name)
		local version = side.versions[resolved]
		return version ~= nil and download_queue_index[resolved .. ":" .. version] ~= nil
	end

	---@param side liveupdater.manifest_side
	---@param file_name string
	---@param processed boolean?
	---@param file_actual boolean
	---@param gating boolean
	local function count_file_side(side, file_name, processed, file_actual, gating)
		local resolved = resolve_archive_name(side, file_name)
		local done = processed and not is_queued(file_name, side) or not processed and file_actual
		count_archive(resolved, side, done, gating)
		local deps = side.dep_lists[file_name]
		if not deps then
			return
		end
		for _, dep_name in ipairs(deps) do
			local dep_resolved = resolve_archive_name(side, dep_name)
			local dep_done
			if processed then
				dep_done = not is_queued(dep_name, side)
			else
				-- mirrors skip_dep in enqueue_modules: a highres copy always
				-- satisfies, a lowres copy satisfies only the lowres pass
				local dep_saved = get_saved_version(dep_name)
				local dep_high = side_archive_version(cache.high, dep_name)
				local dep_low = side_archive_version(cache.low, dep_name)
				dep_done = dep_saved ~= nil
					and ((cache.high ~= nil and dep_saved == dep_high) or (side == cache.low and dep_saved == dep_low))
			end
			local dep_saved_any = get_saved_version(dep_name)
			local dep_satisfied = dep_saved_any ~= nil
				and (
					dep_saved_any == side_archive_version(cache.low, dep_name)
					or dep_saved_any == side_archive_version(cache.high, dep_name)
				)
			count_archive(dep_resolved, side, dep_done, gating, dep_done or dep_satisfied)
		end
	end

	for _, module_name in ipairs(module_names) do
		local module = M.modules[module_name]
		assert(module, "Liveupdater: No such module " .. tostring(module_name))
		local supports_low, supports_high = get_module_resolutions(module)
		local processed = module.processed

		for _, file_name in ipairs(module.files) do
			local saved_version = get_saved_version(file_name)
			local low_version = side_archive_version(cache.low, file_name)
			local high_version = side_archive_version(cache.high, file_name)
			-- same rule as needs_update in enqueue_modules, it gates both resolutions at once
			local file_actual = saved_version ~= nil and (saved_version == low_version or saved_version == high_version)

			if supports_low then
				count_file_side(cache.low, file_name, processed, file_actual, true)
			end
			if supports_high then
				count_file_side(cache.high, file_name, processed, file_actual, not supports_low)
			end
		end
	end

	local low = counters.low
	local high = counters.high
	local result = build_progress(low.total + high.total, low.downloaded + high.downloaded, low.pending + high.pending) --[[@as liveupdater.download_progress]]
	result.low = build_progress(low.total, low.downloaded, low.pending)
	result.high = build_progress(high.total, high.downloaded, high.pending)
	result.gate = build_progress(gate.total, gate.downloaded, gate.pending)
	result.downloading = {}
	for key, state in pairs(in_flight) do
		if state.phase == M.IN_FLIGHT_PHASE.DOWNLOADING then
			table.insert(result.downloading, {
				key = key,
				size = state.item.size or 0,
				bytes = state.bytes or 0,
				in_gate = counted_gate[key] == true,
			})
		end
	end
	return result
end

---Byte-based progress of the current download session, with a per-resolution
---breakdown in the low/high fields (requires manifest with file_sizes).
---@return liveupdater.download_progress
function M.get_download_progress()
	local pending_low = 0
	local pending_high = 0
	for _, item in ipairs(download_files_queue) do
		if item.resolution == M.RESOLUTION.LOW then
			pending_low = pending_low + 1
		else
			pending_high = pending_high + 1
		end
	end

	local partial_low = 0
	local partial_high = 0
	for _, state in pairs(in_flight) do
		if state.phase == M.IN_FLIGHT_PHASE.DOWNLOADING and state.bytes > 0 then
			if state.item.resolution == M.RESOLUTION.LOW then
				partial_low = partial_low + state.bytes
			else
				partial_high = partial_high + state.bytes
			end
		end
	end

	local low = session_bytes.low
	local high = session_bytes.high
	local result = build_progress(
		low.total + high.total,
		low.downloaded + high.downloaded + partial_low + partial_high,
		pending_low + pending_high
	) --[[@as liveupdater.download_progress]]
	result.low = build_progress(low.total, low.downloaded + partial_low, pending_low)
	result.high = build_progress(high.total, high.downloaded + partial_high, pending_high)
	return result
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

---@param lane_or_low number M.PRIORITY_IDS.* alone, or the low priority with high_priority
---@param high_priority number?
---@return number? low, number? high
local function focus_priorities(lane_or_low, high_priority)
	if high_priority == nil then
		local lane = M.PRIORITY[lane_or_low]
		if not lane then
			log.warn("Unknown focus lane: %s", tostring(lane_or_low))
			return nil
		end
		return lane.low, lane.high
	end
	if type(lane_or_low) ~= "number" or type(high_priority) ~= "number" then
		log.warn("focus expects a lane id or low and high priorities")
		return nil
	end
	return lane_or_low, high_priority
end

---@param module_name string
---@param lane_or_low number? M.PRIORITY_IDS.*, or the low priority
---@param high_priority number? required when lane_or_low is a number
function M.focus(module_name, lane_or_low, high_priority)
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

	local force = false
	if lane_or_low ~= nil then
		local low, high = focus_priorities(lane_or_low, high_priority)
		if low == nil or high == nil then
			return
		end
		if not is_module_available(module_name) then
			return
		end
		force = true
		if module.processed then
			local current_low = module.focus_low
			if current_low == nil then
				current_low = module.priority
			end
			local current_high = module.focus_high
			if current_high == nil then
				current_high = module.priority + HIGH_PRIORITY_OFFSET
			end
			if low == current_low and high == current_high then
				return
			end
			module.focus_low = low
			module.focus_high = high
		else
			module.focus_low = low
			module.focus_high = high
			module.by_request = false
		end
	elseif not (module.by_request and not module.processed) then
		return
	elseif not is_module_available(module_name) then
		return
	else
		module.by_request = false
	end

	if enqueue_modules({ [module_name] = module }, force) then
		sort_download_queue()
	end
	if files_load_completed then
		files_load_completed = false
	end
	try_start_load_resources()
end

---@param module_name string
function M.request_module_load(module_name)
	M.focus(module_name)
end

---@param current {state: integer, position: integer?}?
---@param state integer M.STATUS.*
---@param position integer?
---@return {state: integer, position: integer?}
local function worse_status(current, state, position)
	if not current or state < current.state then
		return { state = state, position = position }
	end
	if state == current.state and position and (not current.position or position < current.position) then
		return { state = current.state, position = position }
	end
	return current
end

---@param item liveupdater.queue_item
---@return integer?
local function queue_position(item)
	for index, value in ipairs(download_files_queue) do
		if value == item then
			return index
		end
	end
end

---@param file_name string
---@param resolution liveupdater.resolution
---@return integer state, integer? position
local function archive_status(file_name, resolution)
	local side = resolution == M.RESOLUTION.LOW and manifest_cache.low or manifest_cache.high
	local version, resolved = side_archive_version(side, file_name)
	if not side or not version then
		return M.STATUS.ABSENT, nil
	end
	local key = resolved .. ":" .. version
	local item = download_queue_index[key]
	if item then
		local flight = in_flight[key]
		local state = M.STATUS.QUEUED
		if flight then
			state = flight.phase
		end
		return state, queue_position(item)
	end
	if archive_is_mounting(file_name) then
		return M.STATUS.MOUNTING, nil
	end
	local saved = get_saved_version(file_name)
	if saved == version then
		return M.STATUS.READY, nil
	end
	local high_version = side_archive_version(manifest_cache.high, file_name)
	if resolution == M.RESOLUTION.LOW and saved == high_version then
		return M.STATUS.READY, nil
	end
	local staged = staged_high[resolved]
	if staged and staged.load_info.version == version then
		return M.STATUS.READY, nil
	end
	return M.STATUS.NOT_REQUESTED, nil
end

---@param status {state: integer, position: integer?}
---@param progress liveupdater.resolution_progress
local function status_with_progress(status, progress)
	status.total_bytes = progress.total_bytes
	status.downloaded_bytes = progress.downloaded_bytes
	status.pending_files = progress.pending_files
	status.progress = progress.progress
	return status
end

---@param module liveupdater.module
---@param resolution liveupdater.resolution
---@return {state: integer, position: integer?}
local function resolution_status(module, resolution)
	local supports_low, supports_high = get_module_resolutions(module)
	local supports = (resolution == M.RESOLUTION.LOW and supports_low)
		or (resolution == M.RESOLUTION.HIGH and supports_high)
	if not supports then
		return { state = M.STATUS.ABSENT }
	end
	local side = resolution == M.RESOLUTION.LOW and manifest_cache.low or manifest_cache.high
	if not module.processed then
		for _, file_name in ipairs(module.files) do
			if not side or not side.versions[file_name] then
				return { state = M.STATUS.ABSENT }
			end
		end
		return { state = M.STATUS.NOT_REQUESTED }
	end

	local status = nil
	local mounting = false
	for _, file_name in ipairs(module.files) do
		local state, position = archive_status(file_name, resolution)
		mounting = mounting or state == M.STATUS.MOUNTING
		status = worse_status(status, state, position)
		for _, dep_name in ipairs((side and side.dep_lists[file_name]) or {}) do
			local dep_state, dep_position = archive_status(dep_name, resolution)
			mounting = mounting or dep_state == M.STATUS.MOUNTING
			status = worse_status(status, dep_state, dep_position)
		end
	end
	status = status or { state = M.STATUS.READY }
	status.mounting = mounting
	return status
end

---@param module_name string
---@return {state: integer, position: integer?, low: table, high: table}
function M.get_module_status(module_name)
	assert(M.initialized, "Liveupdater: not initialized")
	assert(M.modules, "Liveupdater: modules not initialized")
	local module = M.modules[module_name]
	assert(module, "Liveupdater: No such module " .. tostring(module_name))

	if not is_liveupdate_available() then
		local idle = build_idle_progress()
		return {
			state = M.STATUS.READY,
			low = status_with_progress({ state = M.STATUS.READY }, idle.low),
			high = status_with_progress({ state = M.STATUS.READY }, idle.high),
		}
	end
	assert(manifest_cache, "Liveupdater: manifest cache not built")

	local progress = M.get_modules_download_progress(module_name)

	local supports_low, supports_high = get_module_resolutions(module)
	local low = status_with_progress(resolution_status(module, M.RESOLUTION.LOW), progress.low)
	local high = status_with_progress(resolution_status(module, M.RESOLUTION.HIGH), progress.high)
	local overall = nil
	if supports_low then
		overall = worse_status(overall, low.state, low.position)
	end
	if supports_high then
		overall = worse_status(overall, high.state, high.position)
	end
	overall = overall or { state = M.STATUS.ABSENT }
	return {
		state = overall.state,
		position = overall.position,
		mounting = (low.mounting or high.mounting) and true or false,
		low = low,
		high = high,
	}
end

---@param module_name string
function M.pin_module(module_name)
	if not M.initialized or not is_liveupdate_available() or not M.modules[module_name] then
		return
	end
	if pinned_modules[module_name] then
		return
	end
	local names = module_archive_names(M.modules[module_name])
	local needs = false
	for _, name in ipairs(names) do
		if archive_awaits_high(name) then
			needs = true
			break
		end
	end
	if not needs then
		return
	end
	pinned_modules[module_name] = names
	for _, name in ipairs(names) do
		pinned_archives[name] = (pinned_archives[name] or 0) + 1
	end
end

---@param module_name string
---@return boolean
function M.is_high_upgrade_ready(module_name)
	if not M.initialized or not is_liveupdate_available() or not pinned_modules[module_name] then
		return false
	end
	local waiting = false
	for _, name in ipairs(pinned_modules[module_name]) do
		if archive_awaits_high(name) then
			waiting = true
			local _, high_version = get_actual_versions(name)
			local staged = staged_high[name]
			if archive_is_mounting(name) or not staged or staged.load_info.version ~= high_version then
				return false
			end
		end
	end
	return waiting
end

---Call only after the collection proxy reading this module is unloaded.
---@param module_name string
---@param cb fun(ok: boolean)
function M.promote_module(module_name, cb)
	local names = pinned_modules[module_name]
	pinned_modules[module_name] = nil
	if names then
		for _, name in ipairs(names) do
			local left = (pinned_archives[name] or 1) - 1
			if left > 0 then
				pinned_archives[name] = left
			else
				pinned_archives[name] = nil
			end
			local staged = staged_high[name]
			if staged and not pinned_archives[name] then
				staged_high[name] = nil
				promote_jobs[#promote_jobs + 1] = staged
			end
		end
	end
	if promote_on_done then
		local previous = promote_on_done
		promote_on_done = function(ok)
			previous(ok)
			if cb then
				cb(ok)
			end
		end
	else
		promote_on_done = cb
	end
	pump_promote()
end

return M
