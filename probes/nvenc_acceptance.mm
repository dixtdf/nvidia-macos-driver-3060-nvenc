// Synthetic hardware acceptance only. No display capture, TCC, network or installation.
#import <Foundation/Foundation.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <VideoToolbox/VideoToolbox.h>
#import <IOKit/IOKitLib.h>
#import <CommonCrypto/CommonDigest.h>
#include <mach-o/dyld.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <regex>
#include <string>
#include <thread>
#include <vector>

using Clock = std::chrono::steady_clock;
static constexpr int Width = 1920, Height = 1080, FPS = 60;
static NSString *const EncoderID = @"com.nvmtl.videoencoder.avc.nvenc";
static constexpr double MaxYMAE = 12, MaxUVMAE = 15, MaxRegionMAE = 24;
static constexpr double MaxThinMAE = 45, MaxMarkerError = 32;

struct Run {
    std::mutex mutex;
    std::condition_variable changed;
    bool encodeDone = false, decodeDone = false;
    OSStatus encodeStatus = 0, decodeStatus = 0;
    VTEncodeInfoFlags encodeFlags = 0;
    VTDecodeInfoFlags decodeFlags = 0;
    CMSampleBufferRef sample = nullptr;
    CVPixelBufferRef decoded = nullptr;
    CMTime decodedPTS = kCMTimeInvalid;
};

static void Encoded(void *context, void *, OSStatus status, VTEncodeInfoFlags flags,
                    CMSampleBufferRef sample) {
    auto &r = *static_cast<Run *>(context);
    std::lock_guard<std::mutex> guard(r.mutex);
    r.encodeStatus = status;
    r.encodeFlags = flags;
    if (sample) { CFRetain(sample); r.sample = sample; }
    r.encodeDone = true;
    r.changed.notify_one();
}

static void Decoded(void *context, void *, OSStatus status, VTDecodeInfoFlags flags,
                    CVImageBufferRef image, CMTime pts, CMTime) {
    auto &r = *static_cast<Run *>(context);
    std::lock_guard<std::mutex> guard(r.mutex);
    r.decodeStatus = status;
    r.decodeFlags = flags;
    if (image) { CFRetain(image); r.decoded = image; }
    r.decodedPTS = pts;
    r.decodeDone = true;
    r.changed.notify_one();
}

static bool Wait(Run &r, bool encode) {
    std::unique_lock<std::mutex> guard(r.mutex);
    return r.changed.wait_for(guard, std::chrono::seconds(3), [&] {
        return encode ? r.encodeDone : r.decodeDone;
    });
}

static uint32_t PCIProperty(io_object_t device, CFStringRef key) {
    CFTypeRef value = IORegistryEntryCreateCFProperty(device, key, kCFAllocatorDefault, 0);
    uint32_t result = 0;
    if (value && CFGetTypeID(value) == CFDataGetTypeID() &&
        CFDataGetLength(static_cast<CFDataRef>(value)) >= 4) {
        uint32_t little = 0;
        std::memcpy(&little, CFDataGetBytePtr(static_cast<CFDataRef>(value)), 4);
        result = CFSwapInt32LittleToHost(little);
    }
    if (value) CFRelease(value);
    return result;
}

static NSDictionary *GPUIdentity() {
    io_iterator_t iterator = IO_OBJECT_NULL;
    unsigned matches = 0, nvidiaDisplays = 0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOPCIDevice"), &iterator) == KERN_SUCCESS) {
        for (io_object_t device; (device = IOIteratorNext(iterator)); IOObjectRelease(device)) {
            if (PCIProperty(device, CFSTR("vendor-id")) == 0x10de &&
                (PCIProperty(device, CFSTR("class-code")) >> 16) == 3) {
                ++nvidiaDisplays;
                if (PCIProperty(device, CFSTR("device-id")) == 0x2504) ++matches;
            }
        }
        IOObjectRelease(iterator);
    }
    return @{ @"detected": @(matches == 1 && nvidiaDisplays == 1), @"matching_devices": @(matches),
              @"nvidia_display_devices": @(nvidiaDisplays),
              @"vendor_id": @(matches ? 0x10de : 0), @"device_id": @(matches ? 0x2504 : 0) };
}

static NSString *LoadedEncoderSHA256() {
    NSString *found = nil;
    for (uint32_t i = 0; i < _dyld_image_count(); ++i) {
        const char *name = _dyld_get_image_name(i);
        if (!name) continue;
        NSString *path = [NSString stringWithUTF8String:name];
        if (![path.lastPathComponent isEqualToString:@"libnvenc_h264.dylib"]) continue;
        if (found) return @""; // Ambiguous libraries cannot qualify.
        NSData *bytes = [NSData dataWithContentsOfFile:path options:NSDataReadingMappedIfSafe error:nil];
        if (!bytes.length || bytes.length > 128 * 1024 * 1024) return @"";
        unsigned char digest[CC_SHA256_DIGEST_LENGTH];
        CC_SHA256(bytes.bytes, static_cast<CC_LONG>(bytes.length), digest);
        NSMutableString *hex = [NSMutableString string];
        for (unsigned char byte : digest) [hex appendFormat:@"%02x", byte];
        found = hex;
    }
    return found ?: @"";
}

static bool Marker(int x, int y, int frame) {
    const int left = 32 + ((frame * 14) % (Width - 192));
    const int top = 32 + ((frame * 6) % (Height - 160));
    return x >= left && x < left + 128 && y >= top && y < top + 96;
}

static bool Fill(CVPixelBufferRef pixel, int frame) {
    static const std::vector<uint8_t> backgroundY = [] {
        std::vector<uint8_t> bytes(Width * Height);
        for (int row = 0; row < Height; ++row) for (int x = 0; x < Width; ++x)
            bytes[row * Width + x] = (x % 128 == 0 || row % 64 == 0) ? 224 :
                static_cast<uint8_t>(48 + ((x / 128 + (row / 64) * 3) % 6) * 28);
        return bytes;
    }();
    static const std::vector<uint8_t> backgroundUV = [] {
        std::vector<uint8_t> bytes(Width * Height / 2);
        for (int row = 0; row < Height / 2; ++row) for (int x = 0; x < Width; x += 2) {
            bytes[row * Width + x] = static_cast<uint8_t>(64 + ((x / 128 + row * 2 / 64) % 4) * 40);
            bytes[row * Width + x + 1] = static_cast<uint8_t>(64 + ((x / 256 + 2 * (row * 2 / 128)) % 4) * 40);
        }
        return bytes;
    }();
    if (CVPixelBufferLockBaseAddress(pixel, 0) != kCVReturnSuccess) return false;
    auto *y = static_cast<uint8_t *>(CVPixelBufferGetBaseAddressOfPlane(pixel, 0));
    auto *uv = static_cast<uint8_t *>(CVPixelBufferGetBaseAddressOfPlane(pixel, 1));
    const size_t ys = CVPixelBufferGetBytesPerRowOfPlane(pixel, 0);
    const size_t uvs = CVPixelBufferGetBytesPerRowOfPlane(pixel, 1);
    for (int row = 0; row < Height; ++row) {
        std::memset(y + row * ys, 16, ys);
        std::memcpy(y + row * ys, backgroundY.data() + row * Width, Width);
    }
    for (int row = 0; row < Height / 2; ++row) {
        std::memset(uv + row * uvs, 128, uvs);
        std::memcpy(uv + row * uvs, backgroundUV.data() + row * Width, Width);
    }
    const int left = 32 + ((frame * 14) % (Width - 192));
    const int top = 32 + ((frame * 6) % (Height - 160));
    for (int row = top; row < top + 96; ++row) std::memset(y + row * ys + left, 210, 128);
    for (int row = top / 2; row < top / 2 + 48; ++row) for (int x = left; x < left + 128; x += 2) {
        uv[row * uvs + x] = 96; uv[row * uvs + x + 1] = 192;
    }
    CVPixelBufferUnlockBaseAddress(pixel, 0);
    return true;
}

struct Pixels { double y = 0, uv = 0, region = 0, thin = 0, marker = 0; };

static bool Verify(CVPixelBufferRef pixel, CVPixelBufferRef reference, int frame, Pixels &out) {
    if (!pixel || CVPixelBufferGetWidth(pixel) != Width || CVPixelBufferGetHeight(pixel) != Height ||
        CVPixelBufferGetPixelFormatType(pixel) != kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ||
        CVPixelBufferGetPlaneCount(pixel) != 2 ||
        CVPixelBufferLockBaseAddress(pixel, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) return false;
    if (CVPixelBufferLockBaseAddress(reference, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) {
        CVPixelBufferUnlockBaseAddress(pixel, kCVPixelBufferLock_ReadOnly); return false;
    }
    const auto *y = static_cast<const uint8_t *>(CVPixelBufferGetBaseAddressOfPlane(pixel, 0));
    const auto *uv = static_cast<const uint8_t *>(CVPixelBufferGetBaseAddressOfPlane(pixel, 1));
    const size_t ys = CVPixelBufferGetBytesPerRowOfPlane(pixel, 0);
    const size_t uvs = CVPixelBufferGetBytesPerRowOfPlane(pixel, 1);
    const auto *expectedY = static_cast<const uint8_t *>(CVPixelBufferGetBaseAddressOfPlane(reference, 0));
    const auto *expectedUV = static_cast<const uint8_t *>(CVPixelBufferGetBaseAddressOfPlane(reference, 1));
    const size_t expectedYS = CVPixelBufferGetBytesPerRowOfPlane(reference, 0);
    const size_t expectedUVS = CVPixelBufferGetBytesPerRowOfPlane(reference, 1);
    uint64_t yError = 0, uvError = 0, thinError = 0, thinCount = 0;
    uint64_t regionError[16] = {}, regionCount[16] = {};
    for (int row = 0; row < Height; ++row) for (int x = 0; x < Width; ++x) {
        const int difference = std::abs(static_cast<int>(y[row * ys + x]) - expectedY[row * expectedYS + x]);
        const int region = std::min(row * 4 / Height, 3) * 4 + x * 4 / Width;
        yError += difference; regionError[region] += difference; ++regionCount[region];
        if ((x % 128 == 0 || row % 64 == 0) && !Marker(x, row, frame)) {
            thinError += difference; ++thinCount;
        }
    }
    for (int row = 0; row < Height / 2; ++row) for (int x = 0; x < Width; x += 2) {
        uvError += std::abs(static_cast<int>(uv[row * uvs + x]) - expectedUV[row * expectedUVS + x]);
        uvError += std::abs(static_cast<int>(uv[row * uvs + x + 1]) - expectedUV[row * expectedUVS + x + 1]);
    }
    const int mx = 32 + ((frame * 14) % (Width - 192)) + 64;
    const int my = 32 + ((frame * 6) % (Height - 160)) + 48;
    out.y = static_cast<double>(yError) / (Width * Height);
    out.uv = static_cast<double>(uvError) / (Width * Height / 2);
    out.thin = static_cast<double>(thinError) / thinCount;
    out.marker = std::max({std::abs(static_cast<int>(y[my * ys + mx]) - 210),
        std::abs(static_cast<int>(uv[(my / 2) * uvs + (mx & ~1)]) - 96),
        std::abs(static_cast<int>(uv[(my / 2) * uvs + (mx & ~1) + 1]) - 192)});
    for (int i = 0; i < 16; ++i)
        out.region = std::max(out.region, static_cast<double>(regionError[i]) / regionCount[i]);
    CVPixelBufferUnlockBaseAddress(pixel, kCVPixelBufferLock_ReadOnly);
    CVPixelBufferUnlockBaseAddress(reference, kCVPixelBufferLock_ReadOnly);
    return out.y <= MaxYMAE && out.uv <= MaxUVMAE && out.region <= MaxRegionMAE &&
           out.thin <= MaxThinMAE && out.marker <= MaxMarkerError;
}

static bool IsIDR(CMSampleBufferRef sample) {
    CMFormatDescriptionRef format = CMSampleBufferGetFormatDescription(sample);
    if (!format || CMFormatDescriptionGetMediaSubType(format) != kCMVideoCodecType_H264) return false;
    int header = 0;
    if (CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format, 0, nullptr, nullptr, nullptr, &header) ||
        header < 1 || header > 4) return false;
    CMBlockBufferRef block = CMSampleBufferGetDataBuffer(sample);
    const size_t size = block ? CMBlockBufferGetDataLength(block) : 0;
    std::vector<uint8_t> bytes(size);
    if (!size || CMBlockBufferCopyDataBytes(block, 0, size, bytes.data())) return false;
    bool idr = false;
    for (size_t position = 0; position < size;) {
        if (size - position < static_cast<size_t>(header)) return false;
        uint32_t length = 0;
        for (int i = 0; i < header; ++i) length = (length << 8) | bytes[position++];
        if (!length || length > size - position) return false;
        if ((bytes[position] & 31) == 5) idr = true;
        position += length;
    }
    return idr;
}

static double Quantile(std::vector<double> samples, double fraction) {
    if (samples.empty()) return 0;
    std::sort(samples.begin(), samples.end());
    return samples[static_cast<size_t>(std::ceil(fraction * samples.size())) - 1];
}

static bool CopyBool(VTSessionRef session, CFStringRef key, bool &value) {
    CFTypeRef result = nullptr;
    const OSStatus status = VTSessionCopyProperty(session, key, nullptr, &result);
    const bool ok = !status && result && CFGetTypeID(result) == CFBooleanGetTypeID();
    value = ok && CFBooleanGetValue(static_cast<CFBooleanRef>(result));
    if (result) CFRelease(result);
    return ok;
}

static bool Number(const char *arg, int &value) {
    char *end = nullptr;
    const long number = std::strtol(arg, &end, 10);
    if (!arg[0] || !end || *end || number < 1 || number > 36000) return false;
    value = static_cast<int>(number); return true;
}

int main(int argc, const char **argv) { @autoreleasepool {
    NSString *reportPath = nil;
    std::string profile = "smoke";
    std::string candidateSHA, upstreamTag;
    int frames = 0, fps = FPS;
    for (int i = 1; i < argc; ++i) {
        const std::string option = argv[i];
        if (i + 1 >= argc) { std::fprintf(stderr, "Each option requires a value.\n"); return 2; }
        const char *value = argv[++i];
        if (option == "--profile") profile = value;
        else if (option == "--candidate-sha256") candidateSHA = value;
        else if (option == "--upstream-tag") upstreamTag = value;
        else if (option == "--frames") { if (!Number(value, frames)) return 2; }
        else if (option == "--fps") { if (!Number(value, fps)) return 2; }
        else if (option == "--report") reportPath = [NSString stringWithUTF8String:value];
        else { std::fprintf(stderr, "Unknown option.\n"); return 2; }
    }
    if ((profile != "smoke" && profile != "qualification") || fps != FPS || !reportPath) return 2;
    const std::regex shaPattern("[0-9a-f]{64}"), tagPattern("v[0-9]+\\.[0-9]+\\.[0-9]+");
    if ((!candidateSHA.empty() && !std::regex_match(candidateSHA, shaPattern)) ||
        (!upstreamTag.empty() && !std::regex_match(upstreamTag, tagPattern)) ||
        (profile == "qualification" && (candidateSHA.empty() || upstreamTag.empty()))) return 2;
    if (!frames) frames = profile == "qualification" ? 3600 : 30;
    if (frames < (profile == "qualification" ? 3600 : 30) ||
        [[NSFileManager defaultManager] fileExistsAtPath:reportPath]) {
        std::fprintf(stderr, "Insufficient frames, or report already exists.\n"); return 2;
    }
    // Bound even a synchronous driver call which never returns. In that case no report is accepted.
    std::mutex watchdogMutex;
    std::condition_variable watchdogCondition;
    bool finished = false;
    std::thread watchdog([&] {
        std::unique_lock<std::mutex> lock(watchdogMutex);
        if (!watchdogCondition.wait_for(lock, std::chrono::seconds(frames / FPS + 30), [&] { return finished; })) {
            std::fputs("probe_watchdog_timeout\n", stderr); std::_Exit(124);
        }
    });
    NSMutableArray *errors = [NSMutableArray array];
    NSMutableDictionary *statuses = [NSMutableDictionary dictionary];
    NSDictionary *gpu = GPUIdentity();
    Run run;
    VTCompressionSessionRef encoder = nullptr;
    VTDecompressionSessionRef decoder = nullptr;
    CVPixelBufferRef input = nullptr;
    NSString *selected = @"";
    NSString *loadedSHA = @"";
    bool hardware = false, decoderHardware = false, decoderQuery = false;
    int encoded = 0, decoded = 0, verified = 0, forced = 0, forcedPassed = 0, idrs = 0;
    int callbackErrors = 0, dropped = 0;
    Pixels maximum;
    std::vector<double> latencies;
    double elapsed = 0;
    auto fail = [&](NSString *code) { [errors addObject:code]; };
    do {
        if (![gpu[@"detected"] boolValue]) { fail(@"target_gpu_missing_or_ambiguous"); break; }
        // Initialize VideoToolbox's hardware plug-in discovery before requesting an exact encoder ID.
        (void)VTIsHardwareDecodeSupported(kCMVideoCodecType_H264);
        NSDictionary *spec = @{
            (__bridge NSString *)kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: @YES,
            (__bridge NSString *)kVTVideoEncoderSpecification_EncoderID: EncoderID };
        NSDictionary *attrs = @{
            (__bridge NSString *)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
            (__bridge NSString *)kCVPixelBufferWidthKey: @(Width),
            (__bridge NSString *)kCVPixelBufferHeightKey: @(Height),
            (__bridge NSString *)kCVPixelBufferIOSurfacePropertiesKey: @{} };
        OSStatus status = VTCompressionSessionCreate(kCFAllocatorDefault, Width, Height, kCMVideoCodecType_H264,
            (__bridge CFDictionaryRef)spec, (__bridge CFDictionaryRef)attrs, nullptr, Encoded, &run, &encoder);
        statuses[@"encoder_create"] = @(status);
        if (status || !encoder) { fail(@"encoder_create_failed"); break; }
        CFTypeRef identity = nullptr;
        status = VTSessionCopyProperty(encoder, kVTCompressionPropertyKey_EncoderID, nullptr, &identity);
        if (!status && identity && CFGetTypeID(identity) == CFStringGetTypeID()) selected = [(__bridge NSString *)identity copy];
        if (identity) CFRelease(identity);
        if (![selected isEqualToString:EncoderID]) { fail(@"encoder_identity_mismatch"); break; }
        loadedSHA = LoadedEncoderSHA256();
        if (!loadedSHA.length) { fail(@"loaded_encoder_hash_unavailable"); break; }
        if (!CopyBool(encoder, kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder, hardware) || !hardware) {
            fail(@"hardware_encoder_not_confirmed"); break;
        }
        NSDictionary *properties = @{
            (__bridge NSString *)kVTCompressionPropertyKey_RealTime: @YES,
            (__bridge NSString *)kVTCompressionPropertyKey_AllowFrameReordering: @NO,
            (__bridge NSString *)kVTCompressionPropertyKey_ExpectedFrameRate: @(FPS),
            (__bridge NSString *)kVTCompressionPropertyKey_AverageBitRate: @20000000,
            (__bridge NSString *)kVTCompressionPropertyKey_MaxKeyFrameInterval: @(FPS),
            (__bridge NSString *)kVTCompressionPropertyKey_MaxFrameDelayCount: @0,
            (__bridge NSString *)kVTCompressionPropertyKey_ProfileLevel: (__bridge NSString *)kVTProfileLevel_H264_High_AutoLevel };
        status = VTSessionSetProperties(encoder, (__bridge CFDictionaryRef)properties);
        statuses[@"encoder_properties"] = @(status);
        if (status) { fail(@"encoder_properties_failed"); break; }
        status = VTCompressionSessionPrepareToEncodeFrames(encoder);
        statuses[@"encoder_prepare"] = @(status);
        if (status) { fail(@"encoder_prepare_failed"); break; }
        const CVReturn pixelStatus = CVPixelBufferCreate(kCFAllocatorDefault, Width, Height,
            kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange, (__bridge CFDictionaryRef)attrs, &input);
        statuses[@"input_buffer"] = @(pixelStatus);
        if (pixelStatus || !input) { fail(@"input_buffer_failed"); break; }
        const auto started = Clock::now();
        for (int frame = 0; frame < frames; ++frame) {
            std::this_thread::sleep_until(started + std::chrono::nanoseconds(1000000000LL * frame / FPS));
            if (!Fill(input, frame)) { fail(@"input_fill_failed"); break; }
            run.encodeDone = run.decodeDone = false;
            run.encodeStatus = run.decodeStatus = 0;
            run.encodeFlags = 0; run.decodeFlags = 0;
            const bool requestIDR = frame % FPS == 0;
            if (requestIDR) ++forced;
            NSDictionary *options = requestIDR ? @{(__bridge NSString *)kVTEncodeFrameOptionKey_ForceKeyFrame: @YES} : nil;
            const auto submitted = Clock::now();
            VTEncodeInfoFlags immediateFlags = 0;
            status = VTCompressionSessionEncodeFrame(encoder, input, CMTimeMake(frame, FPS), CMTimeMake(1, FPS),
                (__bridge CFDictionaryRef)options, nullptr, &immediateFlags);
            if (status) { statuses[@"encode_submit"] = @(status); fail(@"encode_submit_failed"); break; }
            if (!Wait(run, true)) { fail(@"encode_callback_timeout"); break; }
            latencies.push_back(std::chrono::duration<double, std::milli>(Clock::now() - submitted).count());
            if (run.encodeStatus || !run.sample) { ++callbackErrors; fail(@"encode_callback_failed"); break; }
            if ((immediateFlags | run.encodeFlags) & kVTEncodeInfo_FrameDropped) { ++dropped; fail(@"frame_dropped"); break; }
            if (CMTimeCompare(CMSampleBufferGetPresentationTimeStamp(run.sample), CMTimeMake(frame, FPS))) {
                fail(@"encode_timestamp_mismatch"); break;
            }
            ++encoded;
            const bool idr = IsIDR(run.sample);
            if (idr) ++idrs;
            if (requestIDR) {
                if (!idr) { fail(@"forced_idr_missing"); break; }
                ++forcedPassed;
            }
            if (!decoder) {
                VTDecompressionOutputCallbackRecord callback = {Decoded, &run};
                NSDictionary *decodeSpec = @{(__bridge NSString *)kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder: @NO};
                NSDictionary *decodeAttrs = @{(__bridge NSString *)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)};
                status = VTDecompressionSessionCreate(kCFAllocatorDefault, CMSampleBufferGetFormatDescription(run.sample),
                    (__bridge CFDictionaryRef)decodeSpec, (__bridge CFDictionaryRef)decodeAttrs, &callback, &decoder);
                statuses[@"decoder_create"] = @(status);
                if (status || !decoder) { fail(@"decoder_create_failed"); break; }
                decoderQuery = CopyBool(decoder, kVTDecompressionPropertyKey_UsingHardwareAcceleratedVideoDecoder, decoderHardware);
                if (!decoderQuery || decoderHardware) { fail(@"software_decoder_not_confirmed"); break; }
            }
            status = VTDecompressionSessionDecodeFrame(decoder, run.sample, kVTDecodeFrame_EnableAsynchronousDecompression, nullptr, nullptr);
            if (status) { statuses[@"decode_submit"] = @(status); fail(@"decode_submit_failed"); break; }
            if (!Wait(run, false)) { fail(@"decode_callback_timeout"); break; }
            if (run.decodeStatus || !run.decoded || (run.decodeFlags & kVTDecodeInfo_FrameDropped)) {
                ++callbackErrors; fail(@"decode_callback_failed"); break;
            }
            if (CMTimeCompare(run.decodedPTS, CMTimeMake(frame, FPS))) { fail(@"decode_timestamp_mismatch"); break; }
            ++decoded;
            Pixels pixels;
            const bool correct = Verify(run.decoded, input, frame, pixels);
            maximum.y = std::max(maximum.y, pixels.y); maximum.uv = std::max(maximum.uv, pixels.uv);
            maximum.region = std::max(maximum.region, pixels.region); maximum.thin = std::max(maximum.thin, pixels.thin);
            maximum.marker = std::max(maximum.marker, pixels.marker);
            if (!correct) { fail(@"decoded_pixels_incorrect"); break; }
            ++verified;
            CFRelease(run.sample); run.sample = nullptr;
            CFRelease(run.decoded); run.decoded = nullptr;
        }
        elapsed = std::chrono::duration<double>(Clock::now() - started).count();
        if (encoded != frames || decoded != frames || verified != frames) fail(@"frame_count_incomplete");
        if (profile == "qualification" && (elapsed < 59.5 || encoded / elapsed < 59.5 || Quantile(latencies, .95) > 1000.0 / FPS))
            fail(@"sustained_performance_below_target");
    } while (false);
    // Invalidate before destroying callback state. The watchdog also bounds a broken teardown.
    if (encoder) { VTCompressionSessionInvalidate(encoder); CFRelease(encoder); }
    if (decoder) { VTDecompressionSessionInvalidate(decoder); CFRelease(decoder); }
    if (input) CFRelease(input);
    if (run.sample) CFRelease(run.sample);
    if (run.decoded) CFRelease(run.decoded);
    NSDictionary *report = @{
        @"schema_version": @1, @"probe": @"3060-nvenc-h264-v1", @"profile": [NSString stringWithUTF8String:profile.c_str()],
        @"pass": @(errors.count == 0), @"hardware_required": @YES, @"software_fallback_allowed": @NO,
        @"candidate_sha256": [NSString stringWithUTF8String:candidateSHA.c_str()],
        @"upstream_tag": [NSString stringWithUTF8String:upstreamTag.c_str()], @"loaded_encoder_sha256": loadedSHA,
        @"encoder_requested": EncoderID, @"encoder_selected": selected, @"hardware_used": @(hardware), @"gpu": gpu,
        @"video": @{ @"codec": @"h264", @"input_format": @"420v", @"input_layout": @"ordinary_nv12",
            @"width": @(Width), @"height": @(Height), @"fps": @(FPS), @"requested_frames": @(frames) },
        @"results": @{ @"encoded_frames": @(encoded), @"decoded_frames": @(decoded), @"verified_frames": @(verified),
            @"callback_errors": @(callbackErrors), @"dropped_frames": @(dropped), @"forced_keyframes": @(forced),
            @"forced_idr_passed": @(forcedPassed), @"idr_frames": @(idrs), @"decoder_hardware_used": @(decoderHardware),
            @"decoder_hardware_query_success": @(decoderQuery), @"luma_mae_max": @(maximum.y), @"chroma_mae_max": @(maximum.uv),
            @"region_mae_max": @(maximum.region), @"thin_line_mae_max": @(maximum.thin), @"marker_error_max": @(maximum.marker),
            @"elapsed_seconds": @(elapsed), @"encoded_fps": @(elapsed > 0 ? encoded / elapsed : 0),
            @"encode_ms_p50": @(Quantile(latencies, .5)), @"encode_ms_p95": @(Quantile(latencies, .95)),
            @"encode_ms_max": @(latencies.empty() ? 0 : *std::max_element(latencies.begin(), latencies.end())) },
        @"statuses": statuses, @"errors": errors };
    NSError *writeError = nil;
    NSData *json = [NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:&writeError];
    const bool wrote = json && [json writeToFile:reportPath options:NSDataWritingWithoutOverwriting error:&writeError];
    { std::lock_guard<std::mutex> lock(watchdogMutex); finished = true; }
    watchdogCondition.notify_one(); watchdog.join();
    std::fprintf(stderr, "pass=%s encoded=%d decoded=%d verified=%d\n", errors.count ? "false" : "true", encoded, decoded, verified);
    return !wrote ? 2 : errors.count ? 1 : 0;
} }
