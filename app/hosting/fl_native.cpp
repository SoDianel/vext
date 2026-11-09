// SPDX-License-Identifier: GPL-3.0-or-later
// Experimental FL native adapter. No vendor binaries or SDK code.
#define NOMINMAX
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <intrin.h>
#include <utility>
#include <string>
#include <vector>
#include <cmath>
#include <cwctype>
#include <stdlib.h>
#include <atomic>
#include <algorithm>
#include <map>
#include <memory>
#include <commdlg.h>
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "comdlg32.lib")

using VInt = intptr_t;
struct AEffect;
using Dispatch = VInt (*)(AEffect*,int32_t,int32_t,VInt,void*,float);
using Process = void (*)(AEffect*,float**,float**,int32_t);
using SetParam = void (*)(AEffect*,int32_t,float);
using GetParam = float (*)(AEffect*,int32_t);
struct AEffect {
 int32_t magic; Dispatch dispatcher; Process process; SetParam setParameter; GetParam getParameter;
 int32_t programs, params, inputs, outputs, flags; VInt reserved1, reserved2; int32_t latency, realQuality, offlineQuality;
 float ioRatio; void *object, *user; int32_t uniqueID, version; Process processReplacing; void *processDoubleReplacing; char future[56];
};
struct Host { void **vt; int32_t version, flags; void *app; void *waves[10]; void *buffers[4]; int32_t reserved[30]; };
static_assert(offsetof(Host, app)==16, "host layout");
#pragma pack(push, 4)
struct Info { int32_t sdk; const char *longName, *shortName; int32_t flags, numParams, defPoly, numOutCtrls, numOutVoices; };
#pragma pack(pop)
static_assert(offsetof(Info, flags)==20, "packed info layout");
struct Paths { void **vt; };
struct Levels { float pan{}, vol{}, pitch{}, cut{1}, res{}; };
struct VoiceParams { Levels initial, final; };
struct OldLevels { int32_t pan{}; float vol{}; int32_t pitch{}; float cut{1}, res{}; };
struct OldVoiceParams { OldLevels initial, final; };
struct Voice { VoiceParams params{}; OldVoiceParams oldParams{}; VInt handle{}; int channel{}, key{}; bool active{}, held{}, released{}, pendingKill{}, killHandle{}; };
struct MidiEvent { int32_t type, size, offset, flags, length, noteOffset; unsigned char data[4]; char extra[4]; };
struct MidiEvents { int32_t count; VInt reserved; MidiEvent *events[512]; };
#pragma pack(push, 4)
struct SampleInfo { int32_t size; void *data; int32_t length, solidLength, loopStart, loopEnd; double rateConv; int32_t regions; float beats, tempo; int32_t channels, format; int32_t reserved[13]; };
#pragma pack(pop)
static_assert(sizeof(SampleInfo)==108 && offsetof(SampleInfo,length)==12 && offsetof(SampleInfo,regions)==36, "packed sample info layout");
struct SampleBuffer { std::vector<int16_t> pcm; int32_t frames{}, rate{}; std::wstring path; };
struct Context {
 AEffect effect{}; Host host{}; Paths paths{}; void *hostvt[64]{}, *pathvt[64]{};
 HMODULE lib{}; void *native{}; char path[4096]{}; char name[256]{}; int32_t flags{};
 char hostPaths[8][4096]{}, locatedFile[4096]{};
 uint64_t copiedBytes{}; uint32_t copiedFiles{};
 CRITICAL_SECTION pluginLock{}, mixLock{};
 bool audio{}, midi{}, clockOnly{}, clockInitialized{}, keysDataCopied{}, loadNotified{};
 std::atomic<uint64_t> clockStamp{GetTickCount64()};
 Voice voices[128]{};
 bool sustain[16]{};
 float bend[16]{};
 std::vector<MidiEvent> events;
 std::vector<MidiEvent> previewEvents;
 std::vector<float> waves;
 float *renderBuffer{}, *renderInput{};
 bool playing{};
 std::atomic<bool> transport{true};
 std::atomic<double> tempo{100.0};
 std::atomic<int64_t> position{0};
 std::atomic<int32_t> rate{48000};
 int32_t blockSize{4096};
 int32_t maxPoly{128};
 bool hasRendered{}, pendingResetDrain{}, outputSuspended{};
 double nextTick{};
 std::vector<float> input, output, temporary;
 // Host-owned samples: a handle's PCM never moves while the handle is open.
 std::map<VInt,std::unique_ptr<SampleBuffer>> samples;
 VInt nextSample{1}, channelSample{};
 bool sampleRejected{};
 CRITICAL_SECTION sampleLock{};
 Context() { InitializeCriticalSection(&pluginLock); InitializeCriticalSection(&mixLock); InitializeCriticalSection(&sampleLock); }
 ~Context() { DeleteCriticalSection(&sampleLock); DeleteCriticalSection(&mixLock); DeleteCriticalSection(&pluginLock); }
};
static int64_t timeline_position(Context *c) {
 int64_t position=c->position.load();
 if(c->clockOnly && !c->audio && c->transport.load())
  position+=static_cast<int64_t>((GetTickCount64()-c->clockStamp.load())*c->rate.load()/1000);
 return position;
}
static void fail(const char* kind, VInt code) { fprintf(stderr,"unsupported %s %lld\n",kind,(long long)code); fflush(stderr); TerminateProcess(GetCurrentProcess(),86); }
static void log_ascii(const char *label,const char *source) {
 char text[201]{}; size_t count=0; bool ended=false;
 for(;source && count<200;++count) {
  SIZE_T got=0; char ch=0;
  if(!ReadProcessMemory(GetCurrentProcess(),source+count,&ch,1,&got) || got!=1) break;
  if(!ch) { ended=true; break; }
  text[count]=(ch>=32 && ch<127) ? ch : '?';
 }
 text[count]=0;
 fprintf(stderr,"%s bytes=%zu terminated=%d text=%s\n",label,count,ended ? 1 : 0,text); fflush(stderr);
}
static void log_exception_candidate(ULONG_PTR candidate) {
 if(candidate<65536 || candidate>UINTPTR_MAX-8) return;
 void *message=nullptr; SIZE_T got=0;
 if(!ReadProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(candidate+8),&message,sizeof(message),&got)
    || got!=sizeof(message) || reinterpret_cast<uintptr_t>(message)<65536) return;
 wchar_t chars[151]{}; size_t count=0; bool ended=false;
 for(;count<150;++count) {
  wchar_t ch=0;
  if(!ReadProcessMemory(GetCurrentProcess(),static_cast<wchar_t*>(message)+count,&ch,sizeof(ch),&got) || got!=sizeof(ch)) break;
  if(!ch) { ended=true; break; }
  if(ch<32 || ch==127 || (ch>=0xd800 && ch<=0xdfff)) return;
  chars[count]=ch;
 }
 if(!count || !ended) return;
 fprintf(stderr,"exception_message_candidate_unverified object=%p utf16=",reinterpret_cast<void*>(candidate));
 for(size_t i=0;i<count;++i) {
  if(chars[i]>=32 && chars[i]<127 && chars[i]!='\\') fputc(static_cast<char>(chars[i]),stderr);
  else fprintf(stderr,"\\u%04x",static_cast<unsigned>(chars[i]));
 }
 fputc('\n',stderr);
}
static LONG factory_exception_filter(EXCEPTION_POINTERS *exception) {
 auto *record=exception->ExceptionRecord;
 fprintf(stderr,"factory_exception code=0x%08lx argument_count=%lu\n",record->ExceptionCode,record->NumberParameters);
 for(DWORD i=0;i<record->NumberParameters && i<EXCEPTION_MAXIMUM_PARAMETERS;++i) {
  fprintf(stderr,"factory_exception argument[%lu]=0x%llx\n",i,(unsigned long long)record->ExceptionInformation[i]);
  log_exception_candidate(record->ExceptionInformation[i]);
 }
 fflush(stderr); TerminateProcess(GetCurrentProcess(),record->ExceptionCode);
 return EXCEPTION_EXECUTE_HANDLER;
}
using NativeFactory=void* (*)(Host*,VInt);
static void *call_factory(NativeFactory factory,Host *host) {
 __try { return factory(host,0); }
 __except(factory_exception_filter(GetExceptionInformation())) { return nullptr; }
}
static volatile LONG ioEvents=0;
static bool safe_root(const wchar_t *input,std::wstring &root);
static bool shared_codec_path(LPCWSTR requested,std::wstring &resolved) {
 if(!requested)return false;
 wchar_t scratch[32768]{},factory[32768]{},absolute[32768]{};
 DWORD a=GetEnvironmentVariableW(L"VEXT_FL_NATIVE_DATA",scratch,32768),b=GetEnvironmentVariableW(L"VEXT_FL_NATIVE_ROOT",factory,32768);
 if(!a || a>=32768 || !b || b>=32768)return false;
 std::wstring scratchRoot,sharedRoot;
 if(!safe_root(scratch,scratchRoot) || !safe_root((std::wstring(factory)+L"\\..\\..\\Shared").c_str(),sharedRoot))return false;
 DWORD count=GetFullPathNameW(requested,32768,absolute,nullptr);
 if(!count || count>=32768 || _wcsnicmp(absolute,scratchRoot.c_str(),scratchRoot.size()) || absolute[scratchRoot.size()]!=L'\\')return false;
 std::wstring tail=absolute+scratchRoot.size()+1;
 const wchar_t *name=nullptr;
 if(!_wcsicmp(tail.c_str(),L"Plugins\\Fruity\\Effects\\Maximus\\FruityPlug_x64.dll")) {
  std::wstring nativeDir;
  if(!safe_root((std::wstring(factory)+L"\\Effects\\Maximus").c_str(),nativeDir))return false;
  resolved=nativeDir+L"\\Maximus_x64.dll";
  DWORD attrs=GetFileAttributesW(resolved.c_str());
  if(attrs==INVALID_FILE_ATTRIBUTES || (attrs&(FILE_ATTRIBUTE_REPARSE_POINT|FILE_ATTRIBUTE_DIRECTORY)))return false;
  fprintf(stderr,"runtime_dependency legacy_maximus_leaf_alias=1 installed_native=Maximus_x64.dll\n"); fflush(stderr); return true;
 }
 if(!_wcsicmp(tail.c_str(),L"host-path-7\\oggio_x64.dll"))name=L"oggio_x64.dll";
 if(!_wcsicmp(tail.c_str(),L"host-path-7\\EasyFLAC_x64.dll"))name=L"EasyFLAC_x64.dll";
 if(!name)return false;
 resolved=sharedRoot+L"\\"+name;
 DWORD attrs=GetFileAttributesW(resolved.c_str());
 if(attrs==INVALID_FILE_ATTRIBUTES || (attrs&(FILE_ATTRIBUTE_REPARSE_POINT|FILE_ATTRIBUTE_DIRECTORY)))return false;
 fprintf(stderr,"runtime_dependency installed_shared=%ls\n",name); fflush(stderr); return true;
}
static void observe_imports(HMODULE module);
static void observe_vendor_module(HMODULE module) {
 if(!module)return;
 wchar_t root[32768]{},path[32768]{};
 DWORD length=GetEnvironmentVariableW(L"VEXT_FL_NATIVE_ROOT",root,32768);
 if(length && length<32768 && GetModuleFileNameW(module,path,32768) &&
    !_wcsnicmp(root,path,length) && path[length]==L'\\')observe_imports(module);
}
static bool trace_next() { return InterlockedIncrement(&ioEvents)<=256; }
static HANDLE WINAPI trace_file_w(LPCWSTR path,DWORD access,DWORD share,LPSECURITY_ATTRIBUTES sa,DWORD creation,DWORD flags,HANDLE templ) {
 HANDLE result=CreateFileW(path,access,share,sa,creation,flags,templ); DWORD error=GetLastError();
 if(trace_next()) { fprintf(stderr,"io_file_w path=%ls access=%lu creation=%lu error=%lu\n",path,access,creation,result==INVALID_HANDLE_VALUE ? error : 0); fflush(stderr); }
 SetLastError(error); return result;
}
static HANDLE WINAPI trace_file_a(LPCSTR path,DWORD access,DWORD share,LPSECURITY_ATTRIBUTES sa,DWORD creation,DWORD flags,HANDLE templ) {
 HANDLE result=CreateFileA(path,access,share,sa,creation,flags,templ); DWORD error=GetLastError();
 if(trace_next()) { fprintf(stderr,"io_file_a path=%s access=%lu creation=%lu error=%lu\n",path,access,creation,result==INVALID_HANDLE_VALUE ? error : 0); fflush(stderr); }
 SetLastError(error); return result;
}
static HMODULE WINAPI trace_load_w(LPCWSTR path) {
 std::wstring resolved; LPCWSTR effective=shared_codec_path(path,resolved) ? resolved.c_str() : path;
 HMODULE result=LoadLibraryW(effective); DWORD error=GetLastError();
 if(trace_next()) { fprintf(stderr,"io_library_w path=%ls error=%lu\n",path,result ? 0 : error); fflush(stderr); }
 observe_vendor_module(result); SetLastError(error); return result;
}
static HMODULE WINAPI trace_load_a(LPCSTR path) {
 wchar_t wide[32768]{}; std::wstring resolved;
 bool translated=path && MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,path,-1,wide,32768) && shared_codec_path(wide,resolved);
 HMODULE result=translated ? LoadLibraryW(resolved.c_str()) : LoadLibraryA(path); DWORD error=GetLastError();
 if(trace_next()) { fprintf(stderr,"io_library_a path=%s error=%lu\n",path,result ? 0 : error); fflush(stderr); }
 observe_vendor_module(result); SetLastError(error); return result;
}
static HMODULE WINAPI trace_load_ex_w(LPCWSTR path,HANDLE file,DWORD flags) {
 std::wstring resolved; LPCWSTR effective=shared_codec_path(path,resolved) ? resolved.c_str() : path;
 HMODULE result=LoadLibraryExW(effective,file,flags); DWORD error=GetLastError();
 if(trace_next()) { fprintf(stderr,"io_library_ex_w path=%ls error=%lu\n",path,result ? 0 : error); fflush(stderr); }
 if(!(flags&(LOAD_LIBRARY_AS_DATAFILE|LOAD_LIBRARY_AS_IMAGE_RESOURCE)))observe_vendor_module(result);
 SetLastError(error); return result;
}
static LONG CALLBACK trace_exception(EXCEPTION_POINTERS *exception) {
 HMODULE module=nullptr; auto *address=exception->ExceptionRecord->ExceptionAddress;
 if(trace_next() && GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,reinterpret_cast<LPCWSTR>(address),&module)) {
  wchar_t path[32768]{}; GetModuleFileNameW(module,path,32768); const wchar_t *base=wcsrchr(path,L'\\');
  fprintf(stderr,"observed_exception code=0x%08lx module=%ls rva=0x%llx\n",exception->ExceptionRecord->ExceptionCode,base ? base+1 : path,(unsigned long long)(reinterpret_cast<uintptr_t>(address)-reinterpret_cast<uintptr_t>(module))); fflush(stderr);
  if(exception->ExceptionRecord->ExceptionCode==0xc0000005) {
   void *frames[24]{}; USHORT count=CaptureStackBackTrace(0,24,frames,nullptr);
   for(USHORT i=0;i<count;++i) {
    HMODULE owner=nullptr;
    if(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,reinterpret_cast<LPCWSTR>(frames[i]),&owner)) {
     wchar_t filename[32768]{}; GetModuleFileNameW(owner,filename,32768); const wchar_t *leaf=wcsrchr(filename,L'\\');
     fprintf(stderr,"exception_stack frame=%u module=%ls rva=0x%llx\n",i,leaf ? leaf+1 : filename,(unsigned long long)(reinterpret_cast<uintptr_t>(frames[i])-reinterpret_cast<uintptr_t>(owner)));
    }
   }
   fflush(stderr);
  }
 }
 return EXCEPTION_CONTINUE_SEARCH;
}
static void observe_imports(HMODULE module) {
 // Observe selected imports in this disposable target. Resolve only allowlisted
 // installed runtime dependencies; otherwise forward unchanged. Preserve LastError.
 // Original executable bytes stay untouched.
 auto *base=reinterpret_cast<BYTE*>(module); auto *dos=reinterpret_cast<IMAGE_DOS_HEADER*>(base);
 auto *nt=reinterpret_cast<IMAGE_NT_HEADERS64*>(base+dos->e_lfanew);
 auto directory=nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
 if(!directory.VirtualAddress)return;
 auto *descriptor=reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base+directory.VirtualAddress);
 for(;descriptor->Name;++descriptor) {
  if(!descriptor->OriginalFirstThunk)continue;
  auto *name=reinterpret_cast<IMAGE_THUNK_DATA64*>(base+descriptor->OriginalFirstThunk);
  auto *slot=reinterpret_cast<IMAGE_THUNK_DATA64*>(base+descriptor->FirstThunk);
  for(;name->u1.AddressOfData;++name,++slot) {
   if(IMAGE_SNAP_BY_ORDINAL64(name->u1.Ordinal))continue;
   auto *entry=reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base+name->u1.AddressOfData);
   void *replacement=nullptr;
   if(!strcmp(entry->Name,"CreateFileW"))replacement=reinterpret_cast<void*>(trace_file_w);
   if(!strcmp(entry->Name,"CreateFileA"))replacement=reinterpret_cast<void*>(trace_file_a);
   if(!strcmp(entry->Name,"LoadLibraryW"))replacement=reinterpret_cast<void*>(trace_load_w);
   if(!strcmp(entry->Name,"LoadLibraryA"))replacement=reinterpret_cast<void*>(trace_load_a);
   if(!strcmp(entry->Name,"LoadLibraryExW"))replacement=reinterpret_cast<void*>(trace_load_ex_w);
   if(!replacement)continue;
   DWORD old=0,unused=0;
   if(!VirtualProtect(&slot->u1.Function,sizeof(slot->u1.Function),PAGE_READWRITE,&old))fail("observe_import",0);
   slot->u1.Function=reinterpret_cast<ULONGLONG>(replacement);
   VirtualProtect(&slot->u1.Function,sizeof(slot->u1.Function),old,&unused);
  }
 }
}
template<int Slot, bool Path> static void abort_slot() {
 void *caller=_ReturnAddress(); HMODULE module=nullptr;
 if(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       reinterpret_cast<LPCWSTR>(caller),&module)) {
  wchar_t name[32768]{}; DWORD length=GetModuleFileNameW(module,name,32768);
  const wchar_t *base=length ? wcsrchr(name,L'\\') : nullptr;
  fprintf(stderr,"unknown_slot_caller module=%ls rva=0x%llx\n",base ? base+1 : name,
          (unsigned long long)(reinterpret_cast<uintptr_t>(caller)-reinterpret_cast<uintptr_t>(module)));
 } else fprintf(stderr,"unknown_slot_caller module=unknown address=%p\n",caller);
 fflush(stderr); fail(Path ? "path_slot" : "host_slot",Slot);
}
template<bool Path, size_t... I> static void init_slots(void **table, std::index_sequence<I...>) {
 void *entries[] = {reinterpret_cast<void*>(abort_slot<static_cast<int>(I),Path>)...};
 memcpy(table,entries,sizeof(entries));
}
static Context *path_context(Paths* paths) { return reinterpret_cast<Context*>(reinterpret_cast<char*>(paths)-offsetof(Context,paths)); }
static bool read_suffix(const char *source,std::wstring &out,bool &trailing) {
 char raw[1024]{}; SIZE_T got=0; size_t length=0;
 for(;source && length<sizeof(raw)-1;++length) {
  if(!ReadProcessMemory(GetCurrentProcess(),source+length,raw+length,1,&got) || got!=1) return false;
  if(!raw[length]) break;
 }
 if(!source || length==sizeof(raw)-1 || length==0) return false;
 int count=MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,raw,-1,nullptr,0);
 if(!count) return false; std::vector<wchar_t> wide(count);
 if(!MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,raw,-1,wide.data(),count)) return false;
 std::wstring input=wide.data(); for(auto &ch:input) if(ch==L'/') ch=L'\\';
 trailing=input.back()==L'\\'; if(trailing) input.pop_back();
 if(input.empty() || input.front()==L'\\' || input.find(L':')!=std::wstring::npos) return false;
 std::vector<std::wstring> parts; size_t start=0;
 while(start<=input.size()) {
  size_t end=input.find(L'\\',start); auto part=input.substr(start,end==std::wstring::npos ? end : end-start);
  if(part.empty() || part==L"." || part==L".." || part.back()==L'.' || part.back()==L' ') return false;
  parts.push_back(part); if(end==std::wstring::npos) break; start=end+1;
 }
 if(parts.size()<2) return false;
 bool keys=!_wcsicmp(parts[0].c_str(),L"FL Keys") && !_wcsicmp(parts[1].c_str(),L"Wavtables");
 bool bitmap=!_wcsicmp(parts[0].c_str(),L"BeepMap") && !_wcsicmp(parts[1].c_str(),L"Images");
 if(!keys && !bitmap && _wcsicmp(parts[1].c_str(),L"Data") && _wcsicmp(parts[1].c_str(),L"Artwork")) return false;
 out=input; return true;
}
static bool safe_attrs(const std::wstring &path,DWORD *attr=nullptr) {
 DWORD a=GetFileAttributesW(path.c_str());
 if(a==INVALID_FILE_ATTRIBUTES || (a & FILE_ATTRIBUTE_REPARSE_POINT)) return false;
 if(attr) *attr=a; return true;
}
static bool safe_root(const wchar_t *input,std::wstring &root) {
 wchar_t absolute[32768]{}; DWORD n=GetFullPathNameW(input,32768,absolute,nullptr);
 if(!n || n>=32768 || n<3 || absolute[1]!=L':' || absolute[2]!=L'\\') return false;
 root=absolute; std::wstring prefix=root.substr(0,3);
 if(!safe_attrs(prefix)) return false;
 for(size_t start=3;start<root.size();) {
  size_t end=root.find(L'\\',start); if(end==start) { start=end+1; continue; }
  if(prefix.back()!=L'\\') prefix+=L'\\';
  prefix+=root.substr(start,end==std::wstring::npos ? end : end-start);
  DWORD a=0; if(!safe_attrs(prefix,&a) || !(a & FILE_ATTRIBUTE_DIRECTORY)) return false;
  if(end==std::wstring::npos) break; start=end+1;
 }
 return true;
}
static bool allowed_file(const std::wstring &name) {
 auto leaf=name.substr(name.find_last_of(L"\\/")==std::wstring::npos ? 0 : name.find_last_of(L"\\/")+1);
 for(auto &ch:leaf) ch=static_cast<wchar_t>(towlower(ch));
 if(leaf.find(L"license")!=std::wstring::npos || leaf.find(L"licence")!=std::wstring::npos ||
    leaf.find(L"activation")!=std::wstring::npos || leaf.find(L"registration")!=std::wstring::npos) return false;
 auto pos=name.find_last_of(L'.'); if(pos==std::wstring::npos) return false;
 auto ext=name.substr(pos);
 if(!_wcsicmp(ext.c_str(),L".wav") && name.find(L"\\Generators\\FL Keys\\Wavtables\\")!=std::wstring::npos)return true;
 const wchar_t *allowed[]={L".dat",L".txt",L".fns",L".fsc",L".fnv",L".ilcontrol",L".bmp",L".png",L".ini",L".tga",L".dsl",L".wti"};
 for(auto value:allowed) if(!_wcsicmp(ext.c_str(),value)) return true;
 return false;
}
static bool make_safe_dir(const std::wstring &path) {
 if(!CreateDirectoryW(path.c_str(),nullptr) && GetLastError()!=ERROR_ALREADY_EXISTS) return false;
 DWORD a=0; return safe_attrs(path,&a) && (a & FILE_ATTRIBUTE_DIRECTORY);
}
static bool copy_data(Context *c,const std::wstring &source,const std::wstring &destination) {
 WIN32_FILE_ATTRIBUTE_DATA data{};
 if(!GetFileAttributesExW(source.c_str(),GetFileExInfoStandard,&data) || (data.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) return false;
 if(data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
  if(!make_safe_dir(destination)) return false;
  WIN32_FIND_DATAW entry{}; HANDLE search=FindFirstFileW((source+L"\\*").c_str(),&entry);
  if(search==INVALID_HANDLE_VALUE) return GetLastError()==ERROR_FILE_NOT_FOUND;
  bool ok=true;
  do {
   if(!wcscmp(entry.cFileName,L".") || !wcscmp(entry.cFileName,L"..")) continue;
   if(entry.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) continue;
   if(!(entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) && !allowed_file(source+L"\\"+entry.cFileName)) continue;
   if(!copy_data(c,source+L"\\"+entry.cFileName,destination+L"\\"+entry.cFileName)) { ok=false; break; }
  } while(FindNextFileW(search,&entry));
  DWORD error=GetLastError(); FindClose(search); return ok && (error==ERROR_NO_MORE_FILES);
 }
 if(!allowed_file(source)) return false;
 uint64_t bytes=(uint64_t(data.nFileSizeHigh)<<32)|data.nFileSizeLow;
 if(bytes>32ull*1024*1024 || c->copiedBytes+bytes>64ull*1024*1024 || c->copiedFiles>=2000) return false;
 DWORD existing=GetFileAttributesW(destination.c_str());
 if(existing!=INVALID_FILE_ATTRIBUTES && (existing & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY))) return false;
 if(!CopyFileW(source.c_str(),destination.c_str(),FALSE)) return false;
 c->copiedBytes+=bytes; ++c->copiedFiles; return true;
}
static bool factory_resource(Context *c,const char *suffix,void *dest) {
 std::wstring relative; bool trailing=false; if(!read_suffix(suffix,relative,trailing)) return false;
 wchar_t root[32768]{}; DWORD n=GetEnvironmentVariableW(L"VEXT_FL_NATIVE_ROOT",root,32768);
 std::wstring verifiedRoot; if(!n || n>=32768 || !safe_root(root,verifiedRoot)) return false;
 int size=MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,c->path,-1,nullptr,0);
 if(!size) return false; std::vector<wchar_t> scratch(size);
 if(!MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,c->path,-1,scratch.data(),size)) return false;
 std::wstring target=std::wstring(scratch.data())+L"factory-data";
 if(!make_safe_dir(target)) return false;
 for(const wchar_t *section:{L"Generators",L"Effects"}) {
  std::wstring source=verifiedRoot; while(!source.empty() && (source.back()==L'\\' || source.back()==L'/')) source.pop_back();
  source+=L"\\"; source+=section;
  DWORD a=0; if(!safe_attrs(source,&a) || !(a & FILE_ATTRIBUTE_DIRECTORY)) continue;
  std::wstring destination=target+L"\\"+section; if(!make_safe_dir(destination)) return false;
  size_t start=0; bool valid=true;
  while(start<relative.size()) {
   size_t end=relative.find(L'\\',start); auto part=relative.substr(start,end==std::wstring::npos ? end : end-start);
   source+=L"\\"+part; destination+=L"\\"+part;
   if(!safe_attrs(source,&a)) { valid=false; break; }
   if(end!=std::wstring::npos) { if(!(a & FILE_ATTRIBUTE_DIRECTORY) || !make_safe_dir(destination)) { valid=false; break; } start=end+1; }
   else break;
  }
  if(!valid || (trailing && !(a & FILE_ATTRIBUTE_DIRECTORY))) continue;
  // FL Keys' default WTI references another WTI and its adjacent DAT bank.
  // Supply that bounded factory directory together, without user sample paths.
  bool keyBank=!_wcsicmp(section,L"Generators") &&
   (!_wcsicmp(relative.c_str(),L"FL Keys\\Wavtables") || !_wcsnicmp(relative.c_str(),L"FL Keys\\Wavtables\\",18));
  if(keyBank && !trailing && !c->keysDataCopied) {
   if(!copy_data(c,verifiedRoot+L"\\Generators\\FL Keys\\Wavtables",target+L"\\Generators\\FL Keys\\Wavtables"))continue;
   c->keysDataCopied=true;
  }
  if(!copy_data(c,source,destination)) continue;
  if(keyBank)c->keysDataCopied=true;
  if(trailing) destination+=L'\\';
  int bytes=WideCharToMultiByte(CP_ACP,WC_NO_BEST_FIT_CHARS,destination.c_str(),-1,nullptr,0,nullptr,nullptr);
  if(bytes<=0 || bytes>1024) return false;
  std::vector<char> output(bytes); BOOL substituted=FALSE;
  if(!WideCharToMultiByte(CP_ACP,WC_NO_BEST_FIT_CHARS,destination.c_str(),-1,output.data(),bytes,nullptr,&substituted) || substituted) return false;
  SIZE_T written=0; if(!WriteProcessMemory(GetCurrentProcess(),dest,output.data(),bytes,&written) || written!=static_cast<SIZE_T>(bytes)) return false;
  fprintf(stderr,"factory_data copied section=%ls files=%u bytes=%llu\n",section,c->copiedFiles,(unsigned long long)c->copiedBytes); fflush(stderr); return true;
 }
 return false;
}
static bool system_bitmap(Context *c,const char *suffix,void *dest) {
 char name[256]{}; SIZE_T got=0;
 for(size_t i=0;i<sizeof(name)-1;++i) {
  if(!suffix || !ReadProcessMemory(GetCurrentProcess(),suffix+i,name+i,1,&got) || got!=1)return false;
  if(!name[i])break;
 }
 int wideSize=MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,name,-1,nullptr,0);
 if(!wideSize)return false;
 std::vector<wchar_t> chars(wideSize);
 if(!MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,name,-1,chars.data(),wideSize))return false;
 std::wstring relative=chars.data(); for(auto &ch:relative)if(ch==L'/')ch=L'\\';
 if(relative.empty() || relative.front()==L'\\' || relative.find(L':')!=std::wstring::npos)return false;
 auto dot=relative.find_last_of(L'.');
 if(dot==std::wstring::npos || (_wcsicmp(relative.c_str()+dot,L".bmp") && _wcsicmp(relative.c_str()+dot,L".png")))return false;
 for(size_t start=0;start<relative.size();) {
  size_t end=relative.find(L'\\',start); auto part=relative.substr(start,end==std::wstring::npos ? end : end-start);
  if(part.empty() || part==L"." || part==L".." || part.back()==L'.' || part.back()==L' ')return false;
  if(end==std::wstring::npos)break; start=end+1;
 }
 wchar_t root[32768]{}; DWORD count=GetEnvironmentVariableW(L"VEXT_FL_NATIVE_ROOT",root,32768);
 std::wstring verified;
 if(!count || count>=32768 || !safe_root((std::wstring(root)+L"\\..\\..\\Data\\System").c_str(),verified))return false;
 wchar_t scratch[32768]{}; count=GetEnvironmentVariableW(L"VEXT_FL_NATIVE_DATA",scratch,32768);
 if(!count || count>=32768)return false;
 std::wstring destination=std::wstring(scratch)+L"\\system-data";
 if(!make_safe_dir(destination))return false;
 auto sep=relative.find_last_of(L'\\');
 std::wstring source=verified+L"\\"+relative;
 if(sep!=std::wstring::npos) {
  std::wstring ignored;
  if(!safe_root((verified+L"\\"+relative.substr(0,sep)).c_str(),ignored))return false;
  size_t start=0;
  while(start<=sep) {
   size_t end=relative.find(L'\\',start); destination+=L"\\"+relative.substr(start,end-start);
   if(!make_safe_dir(destination))return false;
   start=end+1;
  }
 }
 destination+=L"\\"+relative.substr(sep==std::wstring::npos ? 0 : sep+1);
 if(!copy_data(c,source,destination))return false;
 char output[1024]{}; BOOL substituted=FALSE;
 int bytes=WideCharToMultiByte(CP_ACP,WC_NO_BEST_FIT_CHARS,destination.c_str(),-1,output,sizeof(output),nullptr,&substituted);
 SIZE_T written=0;
 if(bytes<=0 || substituted || !WriteProcessMemory(GetCurrentProcess(),dest,output,bytes,&written) || written!=static_cast<SIZE_T>(bytes))return false;
 fprintf(stderr,"factory_system_bitmap copied=1\n"); fflush(stderr); return true;
}
static int path_lookup(Paths* paths,int32_t selector,const char* suffix,int32_t a,int32_t b,void* dest) {
 fprintf(stderr,"path_lookup selector=%d suffix=%p a=%d b=%d dest=%p\n",selector,suffix,a,b,dest); fflush(stderr);
 log_ascii("path_lookup_suffix",suffix);
 if(selector==1017 || selector==1019 || selector==1020 || selector==1025 || selector==1039 || selector==1067) {
  char empty=0; SIZE_T written=0;
  if(dest && WriteProcessMemory(GetCurrentProcess(),dest,&empty,1,&written) && written==1) {
   if(selector==1025 && a==0 && b==0 && system_bitmap(path_context(paths),suffix,dest))return 1;
   if((selector==1019 || selector==1020) && a>=0 && a<=2 && b>=0 && b<=1 && factory_resource(path_context(paths),suffix,dest)) return 1;
   fprintf(stderr,"path_missing selector=%d\n",selector); fflush(stderr); return 0;
  }
  fail("path_destination",selector);
 }
 fail("path_selector",selector); return 0;
}
// REA evidence in batch/rea-control-0x7ff1b0.json, rea-control-path2.json,
// and rea-control-0x7fee90.json: these callers handle false as missing.
static int path_base(Paths* paths,int32_t selector,uint8_t mode,void* dest) {
 fprintf(stderr,"path_slot0 selector=%d mode=%u dest=%p\n",selector,static_cast<unsigned>(mode),dest); fflush(stderr);
 char empty=0; SIZE_T written=0;
 if((selector==1011 || selector==1020 || selector==1048) && mode<=2) {
  char folder[1024]{};
  if(sprintf_s(folder,"%spath-selector-%d",path_context(paths)->path,selector)<0 ||
     (!CreateDirectoryA(folder,nullptr) && GetLastError()!=ERROR_ALREADY_EXISTS)) fail("scratch_path_selector",selector);
  strcat_s(folder,"\\");
  if(dest && WriteProcessMemory(GetCurrentProcess(),dest,folder,strlen(folder)+1,&written) && written==strlen(folder)+1) {
   fprintf(stderr,"path_base scratch_directory selector=%d\n",selector); fflush(stderr); return 1;
  }
  fail("path_slot0_destination",selector);
 }
 if(dest && WriteProcessMemory(GetCurrentProcess(),dest,&empty,1,&written) && written==1) return 0;
 fail("path_slot0_destination",selector); return 0;
}
static void describe_interface(const char *label,void *object) {
 void **table=nullptr; SIZE_T got=0;
 if(!object || !ReadProcessMemory(GetCurrentProcess(),object,&table,sizeof(table),&got) || got!=sizeof(table))return;
 for(int i=0;i<6;++i) {
  void *entry=nullptr; HMODULE module=nullptr;
  if(!ReadProcessMemory(GetCurrentProcess(),table+i,&entry,sizeof(entry),&got) || got!=sizeof(entry))break;
  if(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,reinterpret_cast<LPCWSTR>(entry),&module)) {
   wchar_t path[32768]{}; GetModuleFileNameW(module,path,32768); const wchar_t *leaf=wcsrchr(path,L'\\');
   fprintf(stderr,"%s slot=%d module=%ls rva=0x%llx\n",label,i,leaf ? leaf+1 : path,(unsigned long long)(reinterpret_cast<uintptr_t>(entry)-reinterpret_cast<uintptr_t>(module)));
  }
 }
 fflush(stderr);
}
static std::string ansi_path(const std::wstring &path) {
 char text[1024]{}; BOOL substituted=FALSE;
 int bytes=WideCharToMultiByte(CP_ACP,WC_NO_BEST_FIT_CHARS,path.c_str(),-1,text,sizeof(text),nullptr,&substituted);
 if(bytes<=0 || substituted)fail("enumeration_ansi_path",0);
 return text;
}
static void gather_files(const std::wstring &dir,const wchar_t *extension,std::vector<std::wstring> &out,int depth=0) {
 if(depth>16 || out.size()>2000)fail("enumeration_bound",depth);
 WIN32_FIND_DATAW entry{}; HANDLE search=FindFirstFileW((dir+L"\\*").c_str(),&entry);
 if(search==INVALID_HANDLE_VALUE)return;
 do {
  if(!wcscmp(entry.cFileName,L".") || !wcscmp(entry.cFileName,L"..") || (entry.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT))continue;
  auto path=dir+L"\\"+entry.cFileName;
  if(entry.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY)gather_files(path,extension,out,depth+1);
  else {
   auto dot=path.find_last_of(L'.');
   if(dot!=std::wstring::npos && !_wcsicmp(path.c_str()+dot,extension))out.push_back(path);
  }
 } while(FindNextFileW(search,&entry));
 FindClose(search);
}
static std::vector<std::wstring> resource_files(Paths *paths,const char *subdir,const wchar_t *extension) {
 char destination[1024]{};
 if(!factory_resource(path_context(paths),subdir,destination))fail("enumeration_resource",0);
 wchar_t wide[1024]{};
 if(!MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,destination,-1,wide,1024))fail("enumeration_resource_encoding",0);
 std::wstring dir=wide; while(!dir.empty() && dir.back()==L'\\')dir.pop_back();
 std::vector<std::wstring> files; gather_files(dir,extension,files); return files;
}
static bool exact_ascii(const char *source,const char *expected) {
 SIZE_T got=0;
 for(size_t i=0;i<=strlen(expected);++i) {
  char value=0;
  if(!source || !ReadProcessMemory(GetCurrentProcess(),source+i,&value,1,&got) || got!=1 || tolower(static_cast<unsigned char>(value))!=tolower(static_cast<unsigned char>(expected[i])))return false;
 }
 return true;
}
static int path_enumerate(Paths* paths,int32_t selector,const char* subdir,const char* extensions,void* arg4,int32_t arg5,int32_t arg6,void* arg7) {
 fprintf(stderr,"path_slot2 selector=%d subdir=%p extensions=%p arg4=%p arg5=%d arg6=%d arg7=%p unavailable=1\n",
         selector,subdir,extensions,arg4,arg5,arg6,arg7); fflush(stderr);
 log_ascii("path_enum_subdir",subdir); log_ascii("path_enum_extensions",extensions); describe_interface("path_enum_receiver",arg4);
 bool drums=arg5==9 && exact_ascii(subdir,"Fruity DrumSynth Live\\Data\\") && exact_ascii(extensions,".DSL");
 bool keys=arg5==79 && exact_ascii(subdir,"FL Keys\\Wavtables\\") && exact_ascii(extensions,".WTI");
 if(selector==1020 && arg4 && arg6==0 && !arg7 && (drums || keys)) {
  auto files=resource_files(paths,subdir,keys ? L".wti" : L".dsl");
  using AddFile=void (*)(void*,const char*,VInt);
  auto callback=reinterpret_cast<AddFile>((*reinterpret_cast<void***>(arg4))[0]);
  for(const auto &file:files) {
   // FL Keys joins a definition stem to Wavtables and appends .wti itself.
   auto value=file;
   if(keys) { value=value.substr(value.find_last_of(L'\\')+1); value=value.substr(0,value.find_last_of(L'.')); }
   auto name=ansi_path(value); callback(arg4,name.c_str(),0);
  }
  fprintf(stderr,"path_enum_definitions delivered=%zu\n",files.size()); fflush(stderr); return !files.empty();
 }
 return 0;
}
// Bounded Dashboard definition enumeration, recovered from its receiver code.
// Other slot-3 requests stop rather than receiving a guessed empty service.
static void path_service_definitions(Paths* paths,int32_t selector,const char* s1,const char* s2,int32_t arg4,void* arg5,int32_t arg6,int32_t arg7) {
 fprintf(stderr,"path_slot3 definition_request selector=%d s1=%p s2=%p arg4=%d arg5=%p arg6=%d arg7=%d\n",
         selector,s1,s2,arg4,arg5,arg6,arg7); fflush(stderr);
 log_ascii("path_service_s1",s1); log_ascii("path_service_s2",s2); describe_interface("path_service_receiver",arg5);
 if(selector==1011 && arg4==0 && arg5 && arg7==8) {
  // This selector addresses an empty scratch user-preset store. Unlike factory
  // definitions, no real user presets are supplied or enumerated by the test.
  char relative[256]{},pattern[64]{}; SIZE_T got=0;
  auto read_text=[&](const char *source,char *dest,size_t length) {
   for(size_t i=0;i<length;++i) {
    if(!source || !ReadProcessMemory(GetCurrentProcess(),source+i,dest+i,1,&got) || got!=1)return false;
    if(!dest[i])return true;
   }
   return false;
  };
  if(!read_text(s1,relative,sizeof(relative)) || !read_text(s2,pattern,sizeof(pattern)))fail("user_preset_path",selector);
  std::string folder=relative;
  auto sep=folder.find('\\');
  if(sep==std::string::npos || (folder.substr(0,sep)!="Effects" && folder.substr(0,sep)!="Generators") ||
     folder.substr(sep+1).empty() || folder.substr(sep+1)=="." || folder.substr(sep+1)==".." ||
     folder.find('\\',sep+1)!=std::string::npos || folder.find_first_of("/:")!=std::string::npos ||
     folder.back()=='.' || folder.back()==' ')fail("user_preset_path",selector);
  if(strncmp(pattern,"*.",2) || !pattern[2])fail("user_preset_pattern",selector);
  for(size_t i=2;pattern[i];++i)if(!isalnum(static_cast<unsigned char>(pattern[i])))fail("user_preset_pattern",selector);
  std::string root=std::string(path_context(paths)->path)+"path-selector-1011";
  for(const auto &dir:{root,root+"\\"+folder.substr(0,sep),root+"\\"+folder}) {
   wchar_t wide[4096]{};
   if(!MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,dir.c_str(),-1,wide,4096) || !make_safe_dir(wide))fail("user_preset_directory",selector);
  }
  WIN32_FIND_DATAA entry{}; HANDLE search=FindFirstFileA((root+"\\"+folder+"\\"+pattern).c_str(),&entry);
  if(search!=INVALID_HANDLE_VALUE) { FindClose(search); fail("user_preset_nonempty_unverified_callback",selector); }
  if(GetLastError()!=ERROR_FILE_NOT_FOUND)fail("user_preset_enumeration",selector);
  fprintf(stderr,"scratch_user_presets verified_empty=1 selector=1011 query_mode=%d\n",arg6); fflush(stderr); return;
 }
 if(selector!=1020 || !arg5 || arg4!=511 || arg6!=0 || arg7!=9 ||
    !exact_ascii(s1,"Dashboard\\Artwork\\") || !exact_ascii(s2,"*.ini"))fail("path_slot3_unverified_request",selector);
 // REA dashboard-0x7c2930 and 0x419520: receiver slot 0 consumes ANSI
 // filename and trailing directory, opens the INI, and builds control entries.
 auto files=resource_files(paths,s1,L".ini");
 using AddDefinition=void (*)(void*,VInt,const char*,const char*);
 auto callback=reinterpret_cast<AddDefinition>((*reinterpret_cast<void***>(arg5))[0]);
 for(const auto &file:files) {
  auto sep=file.find_last_of(L'\\'); auto name=ansi_path(file.substr(sep+1)); auto dir=ansi_path(file.substr(0,sep+1));
  callback(arg5,0,name.c_str(),dir.c_str());
 }
 fprintf(stderr,"path_service_definitions delivered=%zu\n",files.size()); fflush(stderr);
}
static int path_file(Paths*,int32_t selector,const char* dir,const char* file,int32_t a,int32_t b,void* dest) {
 fprintf(stderr,"path_slot7 selector=%d dir=%p file=%p a=%d b=%d dest=%p\n",selector,dir,file,a,b,dest); fflush(stderr);
 char empty=0; SIZE_T written=0;
 if(dest && WriteProcessMemory(GetCurrentProcess(),dest,&empty,1,&written) && written==1) return 0;
 fail("path_slot7_destination",selector); return 0;
}
static void path_sample_location(Paths *paths,const char *file,const char *base,void*,uint8_t *relative) {
 // Edison's Save As asks whether its loaded sample needs a base directory.
 // Its absolute-file branch reads only the final Boolean, not the other output.
 auto *c=path_context(paths);
 char name[4096]{}; SIZE_T got=0;
 for(size_t i=0;i<sizeof(name)-1;++i) {
  if(!file || !ReadProcessMemory(GetCurrentProcess(),file+i,name+i,1,&got) || got!=1)fail("sample_path",0);
  if(!name[i])break;
 }
 char baseFirst=0;
 if(base && (!ReadProcessMemory(GetCurrentProcess(),base,&baseFirst,1,&got) || got!=1))fail("sample_base",0);
 if(strcmp(c->name,"Edison") || baseFirst || strlen(name)<3 ||
    !((name[1]==':' && (name[2]=='\\' || name[2]=='/')) || (name[0]=='\\' && name[1]=='\\')))
  fail("sample_relative_path_unverified",0);
 uint8_t value=0; SIZE_T written=0;
 if(!relative || !WriteProcessMemory(GetCurrentProcess(),relative,&value,1,&written) || written!=1)
  fail("sample_path_destination",0);
}
static Context *context(Host* host) { return reinterpret_cast<Context*>(reinterpret_cast<char*>(host)-offsetof(Context,host)); }
// Callbacks only mark work: native voice methods may call back while rendering.
static void voice_release(Host *host,VInt tag) {
 auto *c=context(host); if(tag<1 || tag>128)return;
 auto &v=c->voices[tag-1]; if(v.active)v.held=false;
}
static void voice_kill(Host *host,VInt tag,int32_t killHandle) {
 auto *c=context(host); if(tag<1 || tag>128)return;
 auto &v=c->voices[tag-1]; if(v.active) { v.pendingKill=true; v.killHandle=killHandle!=0; }
}
static int32_t voice_event(Host*,VInt,VInt,VInt,VInt) { return 0; }
static void controller_changed(Host*,VInt,VInt,VInt) {
 // Internal controller destinations are disconnected in this standalone host.
 // A documented output notification must not terminate an editor on a change.
 // No automation/MIDI output or controller clock is implied by accepting it.
}
static void extra_buffer(Host *host,VInt,VInt index,void *buffer) {
 // The SDK's packed IOBuffer contains a pointer followed by DWORD flags.
 // Playground exposes only its main stereo bus; every additional bus is absent.
 if(!buffer)fail("extra_bus_destination",index);
 memset(buffer,0,sizeof(void*)+sizeof(uint32_t));
 if(index==0)memcpy(buffer,&context(host)->renderBuffer,sizeof(void*));
}
static void input_buffer(Host *host,VInt,VInt index,void *buffer) {
 if(!buffer)fail("input_bus_destination",index);
 auto *c=context(host);
 memset(buffer,0,sizeof(void*)+sizeof(uint32_t));
 if(index==0 && c->renderInput) {
  memcpy(buffer,&c->renderInput,sizeof(void*));
  uint32_t flags=1; memcpy(static_cast<char*>(buffer)+sizeof(void*),&flags,sizeof(flags));
 }
}
static void *unconnected_insert(Host*,VInt,int32_t) { return nullptr; }
static void add_mono_ramp(Host*,float *source,float *dest,int32_t length,float left,float right,float *lastLeft,float *lastRight) {
 if(length<0 || length>4096 || !source || !lastLeft || !lastRight)fail("mono_ramp",length);
 // DrumSynth mixes optional outputs before its main output. An absent extra
 // bus has no destination; advance its gain state without touching any audio.
 if(!dest) { *lastLeft=left; *lastRight=right; return; }
 float startLeft=*lastLeft,startRight=*lastRight;
 for(int i=0;i<length;++i) {
  float phase=(i+1.f)/length;
  dest[2*i]+=source[i]*(startLeft+(left-startLeft)*phase);
  dest[2*i+1]+=source[i]*(startRight+(right-startRight)*phase);
 }
 *lastLeft=left; *lastRight=right;
}
static void add_stereo_ramp(Host*,float *source,float *dest,int32_t length,float left,float right,float *lastLeft,float *lastRight) {
 if(length<0 || length>4096 || !source || !lastLeft || !lastRight)fail("stereo_ramp",length);
 if(!dest) { *lastLeft=left; *lastRight=right; return; }
 float startLeft=*lastLeft,startRight=*lastRight;
 for(int i=0;i<length;++i) {
  float phase=(i+1.f)/length;
  dest[2*i]+=source[2*i]*(startLeft+(left-startLeft)*phase);
  dest[2*i+1]+=source[2*i+1]*(startRight+(right-startRight)*phase);
 }
 *lastLeft=left; *lastRight=right;
}
static double song_ticks(Host *host) {
 auto *c=context(host);
 return static_cast<double>(timeline_position(c))/c->rate.load()*c->tempo.load()/60*96;
}
static int32_t song_ticks_integer(Host *host) { return static_cast<int32_t>(song_ticks(host)); }
static void on_hint(Host*,VInt sender,const char* hint) {
 fprintf(stderr,"host_slot OnHint sender=%lld text_pointer=%p\n",(long long)sender,hint); fflush(stderr);
}
static void on_param_changed(Host*,VInt sender,int32_t index,int32_t value) {
 fprintf(stderr,"host_slot OnParamChanged sender=%lld index=%d value=%d\n",(long long)sender,index,value); fflush(stderr);
}
static void midi_output_unconnected(Host*,VInt sender,VInt message) {
 fprintf(stderr,"host_midi_output unconnected=1 sender=%lld message=%lld\n",(long long)sender,(long long)message); fflush(stderr);
}
static void stereo_levels(float *left,float *right,float pan,float volume,const char *slot) {
 if(!std::isfinite(pan) || !std::isfinite(volume) || volume<0 || !left || !right) fail(slot,0);
 // VEXT uses a center-preserving linear balance. The legacy ABI expresses
 // pan in -64..64; NewVoiceParams expresses the same range as -1..1.
 pan=std::clamp(pan,-1.f,1.f);
 float l=volume*(1-std::max(0.f,pan)),r=volume*(1+std::min(0.f,pan));
 SIZE_T written=0;
 if(!WriteProcessMemory(GetCurrentProcess(),left,&l,sizeof(l),&written) || written!=sizeof(l)) fail(slot,1);
 if(!WriteProcessMemory(GetCurrentProcess(),right,&r,sizeof(r),&written) || written!=sizeof(r)) fail(slot,2);
}
static void compute_lr_old(Host*,float *left,float *right,int32_t pan,float volume) {
 stereo_levels(left,right,pan/64.f,volume,"ComputeLRVolOld");
}
static void compute_lr(Host*,float *left,float *right,float pan,float volume) {
 stereo_levels(left,right,pan,volume,"ComputeLRVol");
}
static void lock_plugin(Host* host,VInt sender) {
 EnterCriticalSection(&context(host)->pluginLock);
}
static void unlock_plugin(Host* host,VInt sender) {
 LeaveCriticalSection(&context(host)->pluginLock);
}
static void lock_mix_old(Host* host) {
 EnterCriticalSection(&context(host)->pluginLock);
}
static void unlock_mix_old(Host* host) {
 LeaveCriticalSection(&context(host)->pluginLock);
}
static void suspend_output(Host* host,VInt sender) {
 auto *c=context(host);
 // Finish any in-flight render before the editor replaces engine data. Do
 // not keep the lock across a loader that can pump its own GUI messages.
 EnterCriticalSection(&c->pluginLock);
 c->outputSuspended=true;
 LeaveCriticalSection(&c->pluginLock);
}
static void resume_output(Host* host,VInt sender) {
 auto *c=context(host);
 EnterCriticalSection(&c->pluginLock);
 c->outputSuspended=false;
 LeaveCriticalSection(&c->pluginLock);
}
static void log_hint(VInt address) {
 char text[201]{}; size_t count=0; bool ended=false;
 for(;address && count<200;++count) {
  SIZE_T read=0; char ch=0;
  if(!ReadProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(address+count),&ch,1,&read) || read!=1) break;
  if(!ch) { ended=true; break; }
  text[count]=(ch>=32 && ch<127) ? ch : '?';
 }
 text[count]=0;
 fprintf(stderr,"hint bytes=%zu terminated=%d text=%s\n",count,ended ? 1 : 0,text); fflush(stderr);
}
static VInt locate_file(Context *c,VInt address) {
 char path[4096]{};
 for(size_t i=0;i<sizeof(path);++i) {
  SIZE_T got=0;
  if(!address || !ReadProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(address+i),path+i,1,&got) || got!=1)return 0;
  if(!path[i]) {
   // The caller already selected an absolute local file. Do not search user
   // folders or network shares, or write a relocated filename into its buffer.
   if(i<3 || !isalpha(static_cast<unsigned char>(path[0])) || path[1]!=':' ||
      (path[2]!='\\' && path[2]!='/') || strchr(path+2,':'))return 0;
   char drive[4]={path[0],':','\\',0};
   UINT type=GetDriveTypeA(drive);
   if(type!=DRIVE_FIXED && type!=DRIVE_REMOVABLE && type!=DRIVE_CDROM)return 0;
   wchar_t wide[4096]{};
   if(!MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,path,-1,wide,4096))return 0;
   for(auto *ch=wide;*ch;++ch)if(*ch==L'/')*ch=L'\\';
   auto *sep=wcsrchr(wide,L'\\'); if(!sep)return 0;
   if(sep>wide+2)*sep=0; else sep[1]=0;
   std::wstring parent;
   if(!safe_root(wide,parent))return 0;
   DWORD attrs=GetFileAttributesA(path);
   if(attrs==INVALID_FILE_ATTRIBUTES || (attrs&(FILE_ATTRIBUTE_DIRECTORY|FILE_ATTRIBUTE_REPARSE_POINT)))return 0;
   strcpy_s(c->locatedFile,path);
   return reinterpret_cast<VInt>(c->locatedFile);
  }
 }
 return 0;
}
// Legacy sample service: stereo PCM16 at the requested rate, decoded from local
// WAV only. Never called with the render lock held; a failure leaves the
// caller's handle unchanged.
static const uint64_t maxSampleFileBytes=512ull<<20;
static const int64_t maxSampleFrames=(256ll<<20)/4;
static bool read_sample_file(const wchar_t *path,std::vector<uint8_t> &bytes) {
 HANDLE file=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_FLAG_SEQUENTIAL_SCAN,nullptr);
 if(file==INVALID_HANDLE_VALUE)return false;
 LARGE_INTEGER size{}; bool ok=GetFileSizeEx(file,&size) && size.QuadPart>=44 && static_cast<uint64_t>(size.QuadPart)<=maxSampleFileBytes;
 if(ok) {
  bytes.resize(static_cast<size_t>(size.QuadPart));
  DWORD got=0; ok=ReadFile(file,bytes.data(),static_cast<DWORD>(bytes.size()),&got,nullptr) && got==bytes.size();
 }
 CloseHandle(file); return ok;
}
static uint32_t le32(const uint8_t *p) { uint32_t v; memcpy(&v,p,4); return v; }
static uint16_t le16(const uint8_t *p) { uint16_t v; memcpy(&v,p,2); return v; }
static bool decode_wav(const std::vector<uint8_t> &b,int32_t rate,SampleBuffer &out) {
 if(memcmp(b.data(),"RIFF",4) || memcmp(b.data()+8,"WAVE",4))return false;
 const uint8_t *fmt=nullptr,*data=nullptr; size_t fmtSize=0,dataSize=0;
 for(size_t at=12;at+8<=b.size();) {
  size_t size=le32(b.data()+at+4),body=at+8;
  if(!memcmp(b.data()+at,"fmt ",4) && size>=16 && body+size<=b.size()) { fmt=b.data()+body; fmtSize=size; }
  else if(!memcmp(b.data()+at,"data",4)) { data=b.data()+body; dataSize=std::min(size,b.size()-body); break; }
  if(size>b.size())break;
  at=body+size+(size&1);
 }
 if(!fmt || !data)return false;
 int tag=le16(fmt),channels=le16(fmt+2),bits=le16(fmt+14);
 int64_t sourceRate=le32(fmt+4);
 if(tag==0xFFFE && fmtSize>=26)tag=le16(fmt+24);
 if((tag!=1 && tag!=3) || channels<1 || channels>32 || sourceRate<4000 || sourceRate>768000)return false;
 if(tag==1 ? (bits!=8 && bits!=16 && bits!=24 && bits!=32) : (bits!=32 && bits!=64))return false;
 size_t stride=channels*(bits/8);
 int64_t frames=static_cast<int64_t>(dataSize/stride);
 if(frames<1)return false;
 auto read=[&](int64_t frame,int ch)->double {
  if(frame<0 || frame>=frames)return 0;
  const uint8_t *p=data+frame*stride+std::min(ch,channels-1)*(bits/8);
  if(tag==3) {
   if(bits==32) { float v; memcpy(&v,p,4); return std::isfinite(v) ? v : 0; }
   double v; memcpy(&v,p,8); return std::isfinite(v) ? v : 0;
  }
  switch(bits) {
   case 8:return (p[0]-128)/128.0;
   case 16:return static_cast<int16_t>(le16(p))/32768.0;
   case 24:return static_cast<int32_t>((static_cast<uint32_t>(le16(p+1))<<16)|(static_cast<uint32_t>(p[0])<<8))/2147483648.0;
   default:return static_cast<int32_t>(le32(p))/2147483648.0;
  }
 };
 double ratio=static_cast<double>(sourceRate)/rate;
 double outputFrames=std::ceil(frames/ratio);
 if(outputFrames<1 || outputFrames>maxSampleFrames)return false;
 out.frames=static_cast<int32_t>(outputFrames); out.rate=rate; out.pcm.resize(static_cast<size_t>(out.frames)*2);
 auto store=[](double v) { return static_cast<int16_t>(std::lround(std::clamp(v,-1.0,1.0)*32767)); };
 if(sourceRate==rate) {
  for(int32_t i=0;i<out.frames;++i)for(int ch=0;ch<2;++ch)out.pcm[2*static_cast<size_t>(i)+ch]=store(read(i,ch));
  return true;
 }
 const double pi=3.14159265358979323846,cutoff=std::min(1.0,1/ratio);
 const int half=static_cast<int>(std::ceil(16/cutoff)),phases=512;
 std::vector<double> table(static_cast<size_t>(phases+1)*2*half);
 for(int phase=0;phase<=phases;++phase)for(int tap=0;tap<2*half;++tap) {
  double x=tap-half+1-static_cast<double>(phase)/phases,arg=pi*x*cutoff;
  double sinc=x==0 ? cutoff : cutoff*std::sin(arg)/arg;
  table[static_cast<size_t>(phase)*2*half+tap]=sinc*(.5+.5*std::cos(pi*x/half));
 }
 for(int32_t i=0;i<out.frames;++i) {
  double center=i*ratio; int64_t whole=static_cast<int64_t>(std::floor(center));
  int phase=static_cast<int>(std::lround((center-whole)*phases));
  const double *weights=&table[static_cast<size_t>(phase)*2*half];
  for(int ch=0;ch<2;++ch) {
   double sum=0;
   for(int tap=0;tap<2*half;++tap)sum+=weights[tap]*read(whole+tap-half+1,ch);
   out.pcm[2*static_cast<size_t>(i)+ch]=store(sum);
  }
 }
 return true;
}
static SampleBuffer *find_sample(Context *c,VInt handle) {
 EnterCriticalSection(&c->sampleLock);
 auto found=c->samples.find(handle);
 auto *sample=found==c->samples.end() ? nullptr : found->second.get();
 LeaveCriticalSection(&c->sampleLock);
 return sample;
}
static VInt store_sample(Context *c,std::unique_ptr<SampleBuffer> buffer) {
 EnterCriticalSection(&c->sampleLock);
 VInt id=c->nextSample++; c->samples[id]=std::move(buffer);
 LeaveCriticalSection(&c->sampleLock);
 return id;
}
static bool pick_sample_file(Context *c,wchar_t *path) {
 OPENFILENAMEW dialog{}; dialog.lStructSize=sizeof(dialog); dialog.hwndOwner=GetAncestor(*reinterpret_cast<HWND*>(static_cast<char*>(c->native)+24),GA_ROOT);
 dialog.lpstrFilter=L"WAV audio (*.wav)\0*.wav\0\0"; dialog.lpstrFile=path; dialog.nMaxFile=4096;
 dialog.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_HIDEREADONLY|OFN_NOCHANGEDIR;
 return GetOpenFileNameW(&dialog)!=FALSE;
}
static void write_sample_name(char *file,const wchar_t *path) {
 char name[256]{};
 if(!WideCharToMultiByte(CP_ACP,WC_NO_BEST_FIT_CHARS,path,-1,name,sizeof(name),nullptr,nullptr))name[0]=0;
 SIZE_T written=0; WriteProcessMemory(GetCurrentProcess(),file,name,strlen(name)+1,&written);
}
static bool load_sample(Host *host,VInt *handle,char *file,const void *format,int32_t flags) {
 auto *c=context(host);
 VInt current=0; SIZE_T got=0;
 if(!handle || !ReadProcessMemory(GetCurrentProcess(),handle,&current,sizeof(current),&got) || got!=sizeof(current))return false;
 struct { uint16_t tag,channels; uint32_t rate,bytes; uint16_t align,bits,extra; } wanted{};
 if(!format || !ReadProcessMemory(GetCurrentProcess(),format,&wanted,18,&got) || got!=18)return false;
 fprintf(stderr,"sample_load flags=%d handle=%lld tag=%u channels=%u rate=%u bits=%u\n",flags,(long long)current,wanted.tag,wanted.channels,wanted.rate,wanted.bits); fflush(stderr);
 if(wanted.tag!=1 || wanted.channels!=2 || wanted.bits!=16 || wanted.align!=4 || wanted.rate<8000 || wanted.rate>192000)return false;
 // Flags 4 converts the channel sample in place: same handle, requested rate,
 // and its name is returned. Replacing any other existing handle is unverified.
 if(flags==4) {
  auto *sample=find_sample(c,current);
  if(!sample || !file)return false;
  if(sample->rate!=wanted.rate) {
   SampleBuffer converted; std::vector<uint8_t> source;
   if(!read_sample_file(sample->path.c_str(),source) || !decode_wav(source,wanted.rate,converted))return false;
   sample->pcm=std::move(converted.pcm); sample->frames=converted.frames; sample->rate=converted.rate;
  }
  write_sample_name(file,sample->path.c_str());
  return true;
 }
 if(current!=0 || (flags & ~1))return false;
 wchar_t path[4096]{};
 if(flags & 1) {
  if(!pick_sample_file(c,path))return false;
 } else {
  if(!file || !locate_file(c,reinterpret_cast<VInt>(file)))return false;
  if(!MultiByteToWideChar(CP_ACP,MB_ERR_INVALID_CHARS,c->locatedFile,-1,path,4096))return false;
 }
 std::vector<uint8_t> bytes; auto buffer=std::make_unique<SampleBuffer>();
 if(!read_sample_file(path,bytes) || !decode_wav(bytes,wanted.rate,*buffer)) {
  fprintf(stderr,"sample_load rejected=1\n"); fflush(stderr); c->sampleRejected=true; return false;
 }
 if((flags & 1) && file)write_sample_name(file,path);
 buffer->path=path;
 *handle=store_sample(c,std::move(buffer));
 return true;
}
static void *sample_data(Host *host,VInt handle,int32_t *length) {
 auto *sample=find_sample(context(host),handle);
 if(!sample || !length)return nullptr;
 *length=sample->frames;
 return sample->pcm.data();
}
static void close_sample(Host *host,VInt handle) {
 auto *c=context(host);
 EnterCriticalSection(&c->sampleLock); c->samples.erase(handle); LeaveCriticalSection(&c->sampleLock);
}
static bool sample_info(Host *host,VInt handle,SampleInfo *info) {
 auto *c=context(host); auto *sample=find_sample(c,handle);
 if(!sample || !info)return false;
 // Plugin-set size bounds the copy: fields past it belong to an older record.
 int32_t size=info->size;
 if(size<=0 || size>static_cast<int32_t>(sizeof(SampleInfo)))size=sizeof(SampleInfo);
 SampleInfo result{}; result.size=info->size; result.data=sample->pcm.data();
 result.length=result.solidLength=sample->frames; result.loopStart=result.loopEnd=-1;
 result.rateConv=static_cast<double>(sample->rate)/c->rate.load();
 result.channels=2;
 memcpy(info,&result,size);
 return true;
}
// No regions are published (SampleInfo.regions is 0), so a request is invalid.
static bool sample_region(Host*,VInt,int32_t,void*) { return false; }
static void show_sample_rejection(Context *c,const char *text,UINT icon) {
 // Plugins report "Title|Body". Only a rejected sample load reaches this box.
 std::string message(text ? text : "");
 auto split=message.find('|');
 std::string title=split==std::string::npos ? "Sample" : message.substr(0,split);
 std::string body=split==std::string::npos ? message : message.substr(split+1);
 MessageBoxA(GetAncestor(*reinterpret_cast<HWND*>(static_cast<char*>(c->native)+24),GA_ROOT),body.c_str(),title.c_str(),MB_OK|icon);
}
static void **native_vt(Context *c);
// FHD_SelectChanSample: the host owns the channel sample and tells the plugin
// through opcode 10. The previous sample stays valid until the plugin moved on.
static VInt select_channel_sample(Context *c) {
 wchar_t path[4096]{};
 if(!pick_sample_file(c,path))return 0;
 std::vector<uint8_t> bytes; auto buffer=std::make_unique<SampleBuffer>();
 if(!read_sample_file(path,bytes) || !decode_wav(bytes,44100,*buffer)) {
  fprintf(stderr,"sample_load rejected=1\n"); fflush(stderr); return 0;
 }
 buffer->path=path;
 VInt id=store_sample(c,std::move(buffer)),previous=c->channelSample;
 suspend_output(&c->host,0);
 reinterpret_cast<VInt (*)(void*,VInt,VInt,VInt)>(native_vt(c)[1])(c->native,10,id,0);
 resume_output(&c->host,0);
 c->channelSample=id;
 if(previous)close_sample(&c->host,previous);
 return 1;
}
static VInt host_dispatch(Host* host,VInt sender,VInt op,VInt index,VInt value) {
 auto *c=context(host);
 if(!c->audio || (op!=16 && op!=36 && op!=37 && op!=38)) {
  fprintf(stderr,"host_dispatch sender=%lld opcode=%lld index=%lld value=%lld\n",(long long)sender,(long long)op,(long long)index,(long long)value); fflush(stderr);
 }
 if(op>=91 || op==12 || op==17) {
  void *caller=_ReturnAddress(); HMODULE module=nullptr;
  if(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,reinterpret_cast<LPCWSTR>(caller),&module)) {
   wchar_t name[32768]{}; GetModuleFileNameW(module,name,32768);
   const wchar_t *base=wcsrchr(name,L'\\');
   fprintf(stderr,"host_callback_caller module=%ls rva=0x%llx\n",base ? base+1 : name,(unsigned long long)(reinterpret_cast<uintptr_t>(caller)-reinterpret_cast<uintptr_t>(module))); fflush(stderr);
  }
 }
 switch(op) {
 case 20:case 21:case 56: {
  // Native editor keyboards ask the host to preview a note; they do not
  // render voices themselves. Queue onto the audio thread, never recurse
  // into the plugin while its GUI callback is active.
  if(!c->midi)return 0;
  int key=static_cast<int>(index & 65535),ch=static_cast<int>((index>>16)&15);
  if(key<0 || key>127)return 0;
  MidiEvent event{}; event.type=1; event.size=sizeof(event);
  bool noteOn=op==20 || (op==56 && value>0);
  event.data[0]=static_cast<unsigned char>((noteOn ? 0x90 : 0x80)|ch);
  event.data[1]=static_cast<unsigned char>(key);
  event.data[2]=static_cast<unsigned char>(op==21 && value<0 ? 0 : std::clamp<VInt>(value,0,127));
  EnterCriticalSection(&c->pluginLock);
  if(!c->outputSuspended && c->previewEvents.size()<512)c->previewEvents.push_back(event);
  LeaveCriticalSection(&c->pluginLock);
  return 0;
 }
 case 30:
  if(value<0 || value>192000*30)fail("latency",value);
  c->effect.latency=static_cast<int32_t>(value); return 0;
 // REA frequency-op91 and callers: a return-unused notification before editor
 // changes, followed by opcode 48 (dirty). This opening-only host records it.
 // Their wider FL meaning remains unverified; this is not a DSP implementation.
 case 91:
  fprintf(stderr,"opening_only_notification opcode=%lld return_unused_observed=1\n",(long long)op); fflush(stderr); return 0;
 case 93:
  if(index==0 && value>=0 && value<=2) {
   // DrumSynth's 0x70bf70 wrapper ignores the return, passing a small state
   // code after changing preset metadata. No host playback exists in this test.
   fprintf(stderr,"opening_only_notification opcode=93 state=%lld\n",(long long)value); fflush(stderr); return 0;
  }
  break;
 case 103:
  // REA Edison RVA 0x48e210: return-unused notification before populating a
  // file menu. No external editor, recent-file or filesystem service implied.
  if(index==0 && value==0 && strcmp(c->name,"Edison")==0)return 0;
  break;
 case 92: {
  // FLEX's 0x7dd488 call supplies a 1025-byte ANSI buffer and converts it to
  // text without using the result. Report unavailable as empty; never invent
  // host identity or authorization. The string's semantic meaning is unknown.
  char empty=0; SIZE_T written=0;
  if(index==0 && value && WriteProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(value),&empty,1,&written) && written==1) {
   fprintf(stderr,"unavailable_private_host_string opcode=92\n"); fflush(stderr); return 0;
  }
  fail("host_string_destination",92); break;
 }
 case 14: {
  VInt result=locate_file(c,value);
  if(!result && !strcmp(c->name,"DirectWave Player")) {
   // This validated loader (RVA 0x42080) immediately measures the returned
   // C string without a null check. Empty still means unavailable: never
   // search for a relative sample or invent a replacement filename.
   c->locatedFile[0]=0;
   return reinterpret_cast<VInt>(c->locatedFile);
  }
  return result;
 }
 case 17:
  // Slicex auto-dumps its slice notes after loading. This standalone host has
  // no piano roll: keep the loaded slices without exporting or replaying notes.
  fprintf(stderr,"piano_roll_export unavailable=1\n"); fflush(stderr); return 0;
 case 15:return 0; // no FL plugin selector dialog in this standalone host
 case 32:return 0; // no external sample editor in this standalone host
 case 12:return select_channel_sample(c);
 case 101:return 0; // channel sample finished loading
 case 65:case 69:return 0; // no host key interception; category menu mode
 case 51:case 52:return 0; // no named external connections in this isolated host
 case 71:return reinterpret_cast<VInt>(&c->paths);
 case 24: {
  HMODULE module=nullptr;
  if(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                        reinterpret_cast<LPCWSTR>(host_dispatch),&module)) return reinterpret_cast<VInt>(module);
  fail("host_module_handle",op); break;
 }
 case 22:log_hint(value); return 0;
 case 2:case 3:case 4:case 5:case 6:case 8:case 9:case 10:case 13:
 case 23:case 27:case 33:case 34:case 35:case 45:case 48:case 49:
 case 55:case 58:case 59:case 67:case 72:case 73:case 86:case 87:return 0;
 case 29:
  if(index==0)return reinterpret_cast<VInt>(c->path);
  // Patcher's Add plugin menu lists the .FST files of this folder (generators
  // in value 0, effects in value 1). Keep it empty until nested loading is validated.
  if(index==2 && !strcmp(c->name,"Patcher") && (value==0 || value==1)) {
   sprintf_s(c->locatedFile,"%spatcher-list\\%s\\",c->path,value ? "effects" : "generators");
   return reinterpret_cast<VInt>(c->locatedFile);
  }
  if(index>0 && index<8) {
   fprintf(stderr,"host_path scratch_only index=%lld\n",(long long)index); fflush(stderr);
   return reinterpret_cast<VInt>(c->hostPaths[index]);
  }
  break;
 case 46:return reinterpret_cast<VInt>(c->host.app);
 case 84:
  // The host owns this palette; these are experimental neutral channel colors,
  // not a claim to reproduce FL's palette. The public API defines -1 as count.
  if(index==0 && value==-1)return 16;
  if(index==0 && value>=0 && value<16) {
   int level=48+static_cast<int>(value)*12;
   return RGB(level,level,level);
  }
  break;
 case 19:
  log_ascii("host_message",reinterpret_cast<const char*>(index));
  if(c->sampleRejected) {
   c->sampleRejected=false;
   show_sample_rejection(c,reinterpret_cast<const char*>(index),static_cast<UINT>(value)&(MB_ICONERROR|MB_ICONWARNING));
   return 0;
  }
  fail("host_message_requires_attention",19); break;
 case 50:if(index==0 || index==1) return 0; break;
 case 16: {
  int32_t ticks=static_cast<int32_t>(std::max<VInt>(0,value));
  int32_t time[3]={ticks/384,(ticks/24)%16,ticks%24}; SIZE_T written=0;
  if(index && WriteProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(index),time,sizeof(time),&written) && written==sizeof(time)) {
   return 0;
  }
  fail("ticks_to_time_destination",op); break;
 }
 case 36:case 37:case 38: {
  double time[2]={}; SIZE_T written=0;
  if((c->audio || c->clockOnly) && op!=38) {
   double seconds=static_cast<double>(timeline_position(c))/c->rate.load();
   switch(index & 255) {
    case 0:time[0]=seconds*c->tempo.load()/60; break; // Playground tempo, in beats
    case 1:case 2:case 3:time[0]=seconds*1000; break;
    case 4:time[0]=seconds*c->tempo.load()/60*96; break; // 96 ticks per quarter note
   }
  }
  if((index & 255)<=4 && value && WriteProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(value),time,sizeof(time),&written) && written==sizeof(time)) {
   return 0;
  }
  fail("time_request",op); break;
 }
 case 26: {
  int32_t flags=0; SIZE_T read=0;
  if(value && ReadProcessMemory(GetCurrentProcess(),reinterpret_cast<void*>(value),&flags,sizeof(flags),&read)
     && read==sizeof(flags) && (flags & 0x8000)) {
   fprintf(stderr,"product_check DENIED trial_flag_preexisting=1 flags_unchanged=1\n"); fflush(stderr); return 0;
  }
  fprintf(stderr,"product_check DENIED precondition_failed\n"); fflush(stderr); fail("product_check_precondition",op); break;
 }
 }
 fail("host_opcode",op); return 0;
}
static void **native_vt(Context *c) { return *reinterpret_cast<void***>(c->native); }
using NativeDispatch=VInt (*)(void*,VInt,VInt,VInt);
using NativeVoid=void (*)(void*);
using NativeRender=void (*)(void*,float*,float*,int32_t);
static VInt native_dispatch(Context *c,VInt op,VInt value) { return reinterpret_cast<NativeDispatch>(native_vt(c)[1])(c->native,op,0,value); }
using VoiceMethod=void (*)(void*,VInt);
static void kill_voice(Context *c,Voice &v) {
 if(!v.active)return;
 auto handle=v.handle; v.active=false; v.pendingKill=false;
 reinterpret_cast<VoiceMethod>(native_vt(c)[11])(c->native,handle);
}
static void reap(Context *c) {
 for(auto &v:c->voices)if(v.active && v.pendingKill) {
  if(v.killHandle)kill_voice(c,v); else { v.active=false; v.pendingKill=false; }
 }
}
static void release_voice(Context *c,Voice &v) {
 if(!v.active || v.released)return;
 v.released=true;
 reinterpret_cast<VoiceMethod>(native_vt(c)[10])(c->native,v.handle);
}
static void stop_voice(Context *c,Voice &v) {
 // A host stop must release before killing: some native instruments keep
 // rendering a voice killed before their release callback has run.
 release_voice(c,v);
 reap(c);
 if(v.active)kill_voice(c,v);
}
static void panic(Context *c) {
 // Reset/suspend is a hard-stop boundary followed by the native flush/resume.
 // Preserve raw kills here; release can make reset audible for a block.
 // DirectWave keeps rendering a voice killed before its release, so it
 // releases first and its reset drain renders the tail away.
 if(!strcmp(c->name,"DirectWave Player"))for(auto &v:c->voices)stop_voice(c,v);
 for(auto &v:c->voices)kill_voice(c,v);
 std::fill_n(c->sustain,16,false); std::fill_n(c->bend,16,0.f); c->events.clear(); c->previewEvents.clear();
}
static bool needs_reset_drain(Context *c) {
 return !strcmp(c->name,"Kepler") || !strcmp(c->name,"Kepler Exo") ||
        !strcmp(c->name,"Transistor Bass") || !strcmp(c->name,"SoundFont Player") ||
        !strcmp(c->name,"DirectWave Player");
}
static void set_native_polyphony(Context *c) {
 if(c->midi)reinterpret_cast<int32_t (*)(void*,int32_t,int32_t,int32_t)>(native_vt(c)[5])
  (c->native,1,c->maxPoly,0);
}
static float voice_pitch(Context *c,int key,int channel) {
 // Zero final pitch is FL's C5, MIDI 60: synths land on A4=440 and samplers
 // on their root (FL Keys' trigger at RVA 0x321660 reads the same origin).
 // 3x Osc's oscillators sound an octave above that zero.
 int origin=!strcmp(c->name,"3x Osc") ? 72 : 60;
 return (key-origin)*100.f+c->bend[channel];
}
static void midi_message(Context *c,const MidiEvent &event) {
 int ch=event.data[0]&15,kind=event.data[0]&240,key=event.data[1]&127,value=event.data[2]&127;
 if(kind==0x90 && value) {
  for(auto &v:c->voices)if(v.active && v.channel==ch && v.key==key)stop_voice(c,v);
  Voice *slot=nullptr; for(auto &v:c->voices)if(!v.active) { slot=&v; break; }
  if(!slot) { slot=&c->voices[0]; stop_voice(c,*slot); }
  *slot=Voice{}; slot->channel=ch; slot->key=key; slot->held=true; slot->active=true;
  slot->params.initial={0,value/127.f,key*100.f,1,0};
  slot->params.final=slot->params.initial; slot->params.final.pitch=voice_pitch(c,key,ch);
  void *params=&slot->params;
  if(!(c->flags&(1<<21))) {
   slot->oldParams.initial={0,value/127.f,key*100,1,0};
   slot->oldParams.final=slot->oldParams.initial;
   slot->oldParams.final.pitch=static_cast<int32_t>(voice_pitch(c,key,ch));
   params=&slot->oldParams;
  }
  slot->handle=reinterpret_cast<VInt (*)(void*,void*,VInt)>(native_vt(c)[9])
   (c->native,params,slot-c->voices+1);
  if(slot->handle==-1)slot->active=false;
 } else if(kind==0x80 || kind==0x90) {
  for(auto &v:c->voices)if(v.active && v.channel==ch && v.key==key) {
   v.held=false; if(!c->sustain[ch])release_voice(c,v);
  }
 } else if(kind==0xb0) {
  if(key==64) {
   c->sustain[ch]=value>=64;
   if(!c->sustain[ch])for(auto &v:c->voices)if(v.active && v.channel==ch && !v.held)release_voice(c,v);
  } else if(key==120 || key==123) {
   for(auto &v:c->voices)if(v.active && v.channel==ch) {
    v.held=false; if(key==120)stop_voice(c,v); else if(!c->sustain[ch])release_voice(c,v);
   }
  }
 } else if(kind==0xe0) {
  c->bend[ch]=((key+(value<<7))-8192)*(200.f/8192);
  for(auto &v:c->voices)if(v.active && v.channel==ch) {
   v.params.final.pitch=voice_pitch(c,v.key,ch);
   v.oldParams.final.pitch=static_cast<int32_t>(voice_pitch(c,v.key,ch));
  }
 }
 reap(c);
}
static void render_generator(Context *c,float *out,int count) {
 c->renderBuffer=out;
 if(c->flags & 4) {
  for(auto &v:c->voices)if(v.active) {
   int length=count;
   reinterpret_cast<int32_t (*)(void*,VInt,float*,int32_t*)>(native_vt(c)[13])(c->native,v.handle,out,&length);
   if(length<0 || length>count)fail("voice_render_length",length);
  }
 } else {
  int length=count;
  reinterpret_cast<void (*)(void*,float*,int32_t*)>(native_vt(c)[8])(c->native,out,&length);
  if(length<0 || length>count)fail("generator_render_length",length);
 }
 reap(c); c->renderBuffer=nullptr;
}
static void drain_reset_cache(Context *c) {
 int64_t position=c->position.load();
 double nextTick=c->nextTick;
 if(c->playing)native_dispatch(c,12,0);
 c->playing=false;
 int32_t divisor=!strcmp(c->name,"Kepler Exo") ? 2 : 4;
 int32_t remaining=std::max(1,c->rate.load()/divisor);
 // DirectWave's released voices finish their envelope after a kill: keep
 // rendering until a block is silent, bounded to ten seconds of audio.
 int32_t tail=!strcmp(c->name,"DirectWave Player") ? c->rate.load()*10 : 0;
 while(remaining>0 || tail>0) {
  int32_t count=std::min({remaining>0 ? remaining : tail,c->blockSize,4096});
  if(count<=0)fail("reset_drain_block",count);
  std::fill_n(c->output.data(),count*2,0.f);
  render_generator(c,c->output.data(),count);
  if(remaining>0)remaining-=count;
  else {
   tail-=count;
   float peak=0; for(int i=0;i<count*2;++i)peak=std::max(peak,std::fabs(c->output[i]));
   if(peak<1e-8f)tail=0;
  }
 }
 // Some engines return their last cached block after suspend. Advance them
 // with silence, then flush once more before accepting newly queued MIDI.
 native_dispatch(c,11,0);
 native_dispatch(c,11,1);
 native_dispatch(c,2,0);
 set_native_polyphony(c);
 c->position.store(position); c->nextTick=nextTick;
 if(position!=0)native_dispatch(c,13,0);
 c->clockStamp.store(GetTickCount64());
 c->pendingResetDrain=false;
}
static void process(AEffect *e,float **input,float **output,int32_t frames) {
 auto *c=static_cast<Context*>(e->object);
 if(!c->audio || frames<0 || frames>4096)fail("audio_block",frames);
 EnterCriticalSection(&c->pluginLock);
 if(c->outputSuspended) {
  std::fill_n(output[0],frames,0.f); std::fill_n(output[1],frames,0.f);
  c->events.clear(); c->previewEvents.clear();
  if(c->transport.load())c->position.fetch_add(frames);
  c->nextTick=static_cast<double>(c->position.load());
  LeaveCriticalSection(&c->pluginLock); return;
 }
 if(frames>0 && c->pendingResetDrain)drain_reset_cache(c);
 for(const auto &event:c->previewEvents)midi_message(c,event);
 c->previewEvents.clear();
 if(c->playing!=c->transport.load()) { c->playing=c->transport.load(); native_dispatch(c,12,c->playing ? 1 : 0); }
 for(int i=0;i<frames;++i) { c->input[2*i]=c->midi ? 0 : input[0][i]; c->input[2*i+1]=c->midi ? 0 : input[1][i]; }
 // Native effects may modify the source or only add to the destination.
 memset(c->output.data(),0,frames*2*sizeof(float));
 int offset=0; size_t eventIndex=0;
 while(offset<frames) {
  while(eventIndex<c->events.size() && c->events[eventIndex].offset<=offset)midi_message(c,c->events[eventIndex++]);
  int count=frames-offset;
  if(eventIndex<c->events.size())count=std::min(count,c->events[eventIndex].offset-offset);
  if((c->flags & 32) && c->playing) {
   double samplesPerTick=static_cast<double>(c->rate.load())/(c->tempo.load()/60*96);
   int64_t position=c->position.load();
   if(position>=static_cast<int64_t>(std::ceil(c->nextTick))) {
    reinterpret_cast<NativeVoid>(native_vt(c)[14])(c->native);
    c->nextTick+=samplesPerTick;
   }
   count=std::min(count,static_cast<int>(std::ceil(c->nextTick)-position));
   if(count<=0)fail("tick_block",count);
  }
  if(c->midi)render_generator(c,c->output.data()+2*offset,count);
  else {
   c->renderInput=c->input.data()+2*offset; c->renderBuffer=c->output.data()+2*offset;
   reinterpret_cast<NativeRender>(native_vt(c)[7])(c->native,c->renderInput,c->renderBuffer,count);
   c->renderInput=nullptr; c->renderBuffer=nullptr;
  }
  if(c->playing)c->position.fetch_add(count); offset+=count;
 }
 c->events.clear();
 if(frames>0)c->hasRendered=true;
 for(int i=0;i<frames;++i) {
  float left=c->output[2*i],right=c->output[2*i+1];
  if(!std::isfinite(left) || !std::isfinite(right))fail("nonfinite_audio",i);
  output[0][i]=left; output[1][i]=right;
 }
 LeaveCriticalSection(&c->pluginLock);
}
// Bad native metadata terminates this disposable child with a clear stage, not the runner.
static bool read_info(Context *c) {
 __try {
  auto info=*reinterpret_cast<Info**>(static_cast<char*>(c->native)+16);
  if(!info || !info->longName) return false;
  c->flags=info->flags;
  if(info->defPoly>0)c->maxPoly=std::min(info->defPoly,128);
  size_t i=0; for(;i<sizeof(c->name)-1 && info->longName[i];++i) c->name[i]=info->longName[i];
  c->name[i]=0;
  fprintf(stderr,"metadata valid=1 sdk=%d flags=%d params=%d name=%s\n",info->sdk,info->flags,info->numParams,c->name); fflush(stderr);
  return i>0;
 } __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static VInt dispatch(AEffect *e,int32_t op,int32_t,VInt value,void* ptr,float opt) {
 auto *c=static_cast<Context*>(e->object);
 if(op!=19) { fprintf(stderr,"vst_dispatch opcode=%d\n",op); fflush(stderr); }
 switch(op) {
 case 0:return 0;
 case 25:if(c->midi && ptr) {
  auto *events=static_cast<MidiEvents*>(ptr);
  if(events->count<0 || events->count>512)fail("midi_event_count",events->count);
  c->events.clear();
  for(int i=0;i<events->count;++i) {
   auto *ev=events->events[i];
   if(ev && ev->type==1 && ev->size>=32 && ev->offset>=0 && ev->offset<4096)c->events.push_back(*ev);
  }
  std::stable_sort(c->events.begin(),c->events.end(),[](const MidiEvent &a,const MidiEvent &b){return a.offset<b.offset;});
 } return 1;
 case 10:if(c->audio) {
  if(!std::isfinite(opt) || opt<8000 || opt>384000)fail("sample_rate",0);
  c->rate.store(static_cast<int32_t>(opt));
  native_dispatch(c,4,c->rate.load());
  // Gross Beat allocates its history ring in SetBlockSize. Its time-signature
  // handler divides by that ring's stride, so initialize the block first.
  native_dispatch(c,3,c->blockSize);
  int32_t sig[3]={16,4,96}; native_dispatch(c,14,reinterpret_cast<VInt>(sig));
  float tick=static_cast<float>(opt/(c->tempo.load()/60*96)),tempo=static_cast<float>(c->tempo.load()); int32_t tickBits=0,tempoBits=0;
  memcpy(&tickBits,&tick,4); memcpy(&tempoBits,&tempo,4);
  native_dispatch(c,20,tickBits);
  reinterpret_cast<int32_t (*)(void*,int32_t,int32_t,int32_t)>(native_vt(c)[5])(c->native,0,tempoBits,static_cast<int32_t>(tick));
 } return 0;
 case 11:if(c->audio) {
  if(value<=0 || value>4096)fail("block_size",value);
  c->blockSize=static_cast<int32_t>(value); native_dispatch(c,3,value);
 } return 0;
 case 12:if(c->audio) {
  if(c->midi)panic(c);
  if(c->playing)native_dispatch(c,12,0);
  c->playing=false;
  native_dispatch(c,11,value ? 1 : 0);
  if(value) {
   native_dispatch(c,2,0);
   // GMS and related standalone engines otherwise retain a zero voice limit.
   // Respect their preferred limit within the host's bounded voice pool.
   set_native_polyphony(c);
   c->position.store(0); c->nextTick=0;
   c->pendingResetDrain=c->midi && c->hasRendered && needs_reset_drain(c);
  } else c->pendingResetDrain=false;
 } return 0;
 case 1:
  if(c->midi)panic(c);
  fprintf(stderr,"stage native_destroy\n"); fflush(stderr);
  reinterpret_cast<NativeVoid>(native_vt(c)[0])(c->native);
  fprintf(stderr,"stage native_destroy_returned\n"); fflush(stderr);
  FreeLibrary(c->lib);
  fprintf(stderr,"stage native_unloaded\n"); fflush(stderr); delete c; return 0;
 case 14:
  if(!c->audio && !c->clockInitialized && !strcmp(c->name,"Newtone")) {
   // Its file-analysis editor queries signature/tempo before any audio stream.
   // Supply static metadata without transport, ticks, voices or processing.
   native_dispatch(c,4,c->rate.load());
   int32_t sig[3]={16,4,96}; native_dispatch(c,14,reinterpret_cast<VInt>(sig));
   float tick=static_cast<float>(c->rate.load()/(c->tempo.load()/60*96));
   float tempo=static_cast<float>(c->tempo.load()); int32_t tickBits=0,tempoBits=0;
   memcpy(&tickBits,&tick,4); memcpy(&tempoBits,&tempo,4);
   native_dispatch(c,20,tickBits);
   reinterpret_cast<int32_t (*)(void*,int32_t,int32_t,int32_t)>(native_vt(c)[5])
    (c->native,0,tempoBits,static_cast<int32_t>(tick));
   c->clockInitialized=true;
  }
  fprintf(stderr,"stage editor_open\n"); fflush(stderr); native_dispatch(c,0,reinterpret_cast<VInt>(ptr));
  // REA FL Keys dispatcher 40 starts the selected bank's sample loader; its
  // dispatcher 56 reports loading until the GUI receives the completion message.
  if(c->audio && !c->loadNotified && !strcmp(c->name,"FL Keys")) {
   c->loadNotified=true; native_dispatch(c,40,0);
  }
  return 1;
 case 15:fprintf(stderr,"stage editor_close\n"); fflush(stderr); native_dispatch(c,0,0); return 0;
 case 19:reinterpret_cast<NativeVoid>(native_vt(c)[2])(c->native); return 0;
 case 35:return (c->flags&1) ? 2 : 1;
 case 45:case 48:if(ptr) strncpy_s(static_cast<char*>(ptr),64,c->name,_TRUNCATE); return 1;
 default:(void)value; return 0;
 }
}
extern "C" __declspec(dllexport) void VEXTNativeSetTransport(AEffect *e,double bpm,int32_t playing,int64_t position) {
 if(!e || !e->object || !std::isfinite(bpm) || bpm<30 || bpm>300)return;
 auto *c=static_cast<Context*>(e->object);
 EnterCriticalSection(&c->pluginLock);
 if(c->clockOnly && !c->audio) {
  c->position.store(timeline_position(c)); c->clockStamp.store(GetTickCount64());
  if(!c->clockInitialized) {
   native_dispatch(c,4,c->rate.load());
   int32_t sig[3]={16,4,96}; native_dispatch(c,14,reinterpret_cast<VInt>(sig));
  }
 }
 if((c->tempo.exchange(bpm)!=bpm || (c->clockOnly && !c->clockInitialized)) && (c->audio || c->clockOnly)) {
  float tick=static_cast<float>(c->rate.load()/(bpm/60*96)),tempo=static_cast<float>(bpm);
  int32_t tickBits=0,tempoBits=0; memcpy(&tickBits,&tick,4); memcpy(&tempoBits,&tempo,4);
  native_dispatch(c,20,tickBits);
  reinterpret_cast<int32_t (*)(void*,int32_t,int32_t,int32_t)>(native_vt(c)[5])(c->native,0,tempoBits,static_cast<int32_t>(tick));
  c->nextTick=static_cast<double>(c->position.load());
 }
 c->clockInitialized=true;
 bool jumped=false;
 if(position>=0) {
  jumped=std::llabs(c->position.load()-position)>1;
  c->position.store(position); c->clockStamp.store(GetTickCount64());
  if(jumped)c->nextTick=static_cast<double>(position);
 }
 // Notifications may query host time immediately (Big Clock latches on pause).
 c->transport.store(playing!=0);
 if(c->playing!=(playing!=0) && (c->audio || c->clockOnly)) { c->playing=playing!=0; native_dispatch(c,12,playing!=0); }
 if(jumped && (c->audio || c->clockOnly))native_dispatch(c,13,0);
 LeaveCriticalSection(&c->pluginLock);
}
extern "C" __declspec(dllexport) HWND VEXTNativeEditorHandle(AEffect *e) {
 if(!e || !e->object) return nullptr;
 auto *c=static_cast<Context*>(e->object);
 return *reinterpret_cast<HWND*>(static_cast<char*>(c->native)+24);
}
extern "C" __declspec(dllexport) int32_t VEXTNativeLoading(AEffect *e) {
 if(!e || !e->object)return 0;
 auto *c=static_cast<Context*>(e->object);
 return c->loadNotified && native_dispatch(c,56,0)!=0;
}
extern "C" __declspec(dllexport) AEffect* VSTPluginMain(void*) {
 wchar_t dll[32768]; DWORD n=GetEnvironmentVariableW(L"VEXT_FL_NATIVE_DLL",dll,32768);
 if(!n || n>=32768) { fprintf(stderr,"missing explicit native DLL\n"); return nullptr; }
 const wchar_t *basename=dll;
 for(const wchar_t *p=dll;*p;++p)if(*p==L'\\' || *p==L'/')basename=p+1;
 if(_wcsicmp(basename,L"Fruity Voltage Controller_x64.dll")==0) {
  fprintf(stderr,"Voltage/CV controller blocked before DLL load\n"); return nullptr;
 }
 auto *c=new Context();
 init_slots<false>(c->hostvt,std::make_index_sequence<64>{}); init_slots<true>(c->pathvt,std::make_index_sequence<64>{});
 c->hostvt[0]=reinterpret_cast<void*>(host_dispatch); c->hostvt[1]=reinterpret_cast<void*>(on_param_changed);
 c->hostvt[2]=reinterpret_cast<void*>(on_hint); c->hostvt[3]=reinterpret_cast<void*>(compute_lr_old);
 c->hostvt[4]=reinterpret_cast<void*>(voice_release); c->hostvt[5]=reinterpret_cast<void*>(voice_kill);
 c->hostvt[19]=reinterpret_cast<void*>(controller_changed);
 c->hostvt[6]=reinterpret_cast<void*>(voice_event);
 c->hostvt[7]=reinterpret_cast<void*>(lock_mix_old); c->hostvt[8]=reinterpret_cast<void*>(unlock_mix_old);
 c->hostvt[9]=reinterpret_cast<void*>(midi_output_unconnected); c->hostvt[10]=reinterpret_cast<void*>(midi_output_unconnected);
 c->hostvt[16]=reinterpret_cast<void*>(song_ticks_integer);
 c->hostvt[17]=reinterpret_cast<void*>(song_ticks); c->hostvt[18]=reinterpret_cast<void*>(song_ticks);
 c->hostvt[11]=reinterpret_cast<void*>(add_mono_ramp); c->hostvt[26]=reinterpret_cast<void*>(unconnected_insert);
 c->hostvt[12]=reinterpret_cast<void*>(add_stereo_ramp);
 c->hostvt[36]=reinterpret_cast<void*>(input_buffer); c->hostvt[37]=reinterpret_cast<void*>(extra_buffer);
 c->hostvt[31]=reinterpret_cast<void*>(compute_lr); c->hostvt[32]=reinterpret_cast<void*>(lock_plugin); c->hostvt[33]=reinterpret_cast<void*>(unlock_plugin);
 c->hostvt[13]=reinterpret_cast<void*>(load_sample); c->hostvt[14]=reinterpret_cast<void*>(sample_data);
 c->hostvt[15]=reinterpret_cast<void*>(close_sample); c->hostvt[23]=reinterpret_cast<void*>(sample_info);
 c->hostvt[30]=reinterpret_cast<void*>(sample_region);
 c->hostvt[43]=reinterpret_cast<void*>(suspend_output); c->hostvt[44]=reinterpret_cast<void*>(resume_output);
 c->pathvt[0]=reinterpret_cast<void*>(path_base); c->pathvt[2]=reinterpret_cast<void*>(path_enumerate);
 c->pathvt[3]=reinterpret_cast<void*>(path_service_definitions);
 c->pathvt[6]=reinterpret_cast<void*>(path_lookup); c->pathvt[7]=reinterpret_cast<void*>(path_file);
 c->pathvt[11]=reinterpret_cast<void*>(path_sample_location);
 c->host.vt=c->hostvt; c->host.version=26000000; c->paths.vt=c->pathvt;
 char parentText[32]{};
 if(GetEnvironmentVariableA("VEXT_FL_NATIVE_PARENT_HWND",parentText,sizeof(parentText))) {
  auto parent=reinterpret_cast<HWND>(_strtoui64(parentText,nullptr,10)); DWORD owner=0;
  if(!IsWindow(parent) || !GetWindowThreadProcessId(parent,&owner) || owner!=GetCurrentProcessId())fail("host_parent_window",0);
  c->host.app=parent;
 }
 DWORD p=GetEnvironmentVariableA("VEXT_FL_NATIVE_DATA",c->path,sizeof(c->path)-2);
 if(!p || p>=sizeof(c->path)-2) { delete c; return nullptr; } c->path[p]='\\'; c->path[p+1]=0;
 for(int i=1;i<8;++i) {
  if(sprintf_s(c->hostPaths[i],"%shost-path-%d",c->path,i)<0 ||
     (!CreateDirectoryA(c->hostPaths[i],nullptr) && GetLastError()!=ERROR_ALREADY_EXISTS)) fail("scratch_host_path",i);
  strcat_s(c->hostPaths[i],"\\");
 }
 // Fixed host tables must exist before a native constructor can cache them.
 c->clockOnly=GetEnvironmentVariableA("VEXT_FL_NATIVE_CLOCK",nullptr,0)>0;
 if(c->clockOnly)c->transport.store(false);
 c->midi=GetEnvironmentVariableA("VEXT_FL_NATIVE_MIDI",nullptr,0)>0;
 c->audio=GetEnvironmentVariableA("VEXT_FL_NATIVE_AUDIO",nullptr,0)>0;
 if(c->audio) {
  if(c->midi) {
   c->waves.resize(6*16384);
   uint32_t noise=0x12345678;
   for(int i=0;i<16384;++i) {
    float phase=i/16384.f;
    noise=1664525*noise+1013904223;
    c->waves[i]=std::sin(phase*6.28318530718f);
    c->waves[16384+i]=1-4*std::abs(phase-.5f);
    c->waves[32768+i]=phase<.5f ? 1.f : -1.f;
    c->waves[49152+i]=2*phase-1;
    c->waves[65536+i]=2*phase-1;
    c->waves[81920+i]=static_cast<float>(noise)/2147483648.f-1;
   }
   for(int i=0;i<6;++i)c->host.waves[i]=c->waves.data()+16384*i;
  }
  c->input.resize(4096*2); c->output.resize(4096*2); c->temporary.resize(4096*2*4);
  for(int i=0;i<4;++i)c->host.buffers[i]=c->temporary.data()+4096*2*i;
 }
 fprintf(stderr,"stage load_library\n"); fflush(stderr);
 c->lib=LoadLibraryW(dll); if(!c->lib) { fprintf(stderr,"load_library failed=%lu\n",GetLastError()); delete c; return nullptr; }
 if(GetEnvironmentVariableA("VEXT_FL_NATIVE_TRACE",nullptr,0))AddVectoredExceptionHandler(1,trace_exception);
 observe_imports(c->lib);
 auto factory=reinterpret_cast<NativeFactory>(GetProcAddress(c->lib,"CreatePlugInstance"));
 if(!factory) { fprintf(stderr,"factory_missing\n"); FreeLibrary(c->lib); delete c; return nullptr; }
 fprintf(stderr,"stage factory_call\n"); fflush(stderr); c->native=call_factory(factory,&c->host);
 fprintf(stderr,"stage factory_returned pointer=%p\n",c->native); fflush(stderr);
 if(!c->native) { FreeLibrary(c->lib); delete c; return nullptr; }
 fprintf(stderr,"stage metadata\n"); fflush(stderr);
 if(!read_info(c)) { fprintf(stderr,"metadata valid=0\n"); fflush(stderr); fail("invalid_metadata",0); }
 if(c->midi && (!(c->flags&1) || !(c->flags&16) || (c->flags&64)))fail("unsupported_instrument",c->flags);
 if(c->audio && (c->flags&64 || ((c->flags&1) && !c->midi)))fail("not_an_audio_effect",c->flags);
 if(c->midi && (c->flags & 8))native_dispatch(c,10,reinterpret_cast<VInt>(c->waves.data()));
 c->effect.magic=0x56737450; c->effect.dispatcher=dispatch; c->effect.params=0; c->effect.programs=1;
 c->effect.inputs=(c->flags&1) ? 0 : 2; c->effect.outputs=2; c->effect.flags=1 | ((c->flags&1) ? (1<<8) : 0);
 c->effect.object=c; c->effect.uniqueID=0x56665432; c->effect.version=1;
 if(c->audio) {
  c->effect.flags|=1<<4; c->effect.process=process; c->effect.processReplacing=process;
 }
 return &c->effect;
}

extern "C" __declspec(dllexport) int VEXTNativeAdapterVersion() { return 4; }
