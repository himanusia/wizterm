// wiz_tap.swift - stream system or per-app audio to stdout as raw float32.
//
// macOS 14.4+ Core Audio process tap (CATapDescription +
// AudioHardwareCreateProcessTap), the same approach Atoll uses. No virtual
// loopback driver (BlackHole/Loopback) is required.
//
// Usage:
//   wiz-tap [--process NAME]... [--list] [--seconds N]
//
//   --process NAME   tap only these apps (repeatable, matches the process name
//                    exactly, e.g. Spotify); default taps all system audio
//   --list           print candidate audio processes and exit
//   --seconds N      stop after N seconds (default: run until killed)
//
// Writes little-endian float32 mono samples to stdout, non-blocking so a slow
// reader drops audio instead of stalling the real-time thread. The first line
// on stderr is always "RATE <hz>" once the tap is live.
//
// Build:  swiftc -O -o wiz-tap wiz_tap.swift

import AudioToolbox
import CoreAudio
import Foundation

nonisolated(unsafe) var gDropCount: UInt64 = 0
nonisolated(unsafe) var gBufferCount: Int32 = 1
nonisolated(unsafe) var gChannels: UInt32 = 0

func fail(_ message: String, _ status: OSStatus = 0) -> Never {
    if status != 0 {
        fputs("error: \(message) (OSStatus \(status))\n", stderr)
    } else {
        fputs("error: \(message)\n", stderr)
    }
    exit(1)
}

func processObject(for pid: pid_t) -> AudioObjectID? {
    var pidValue = pid
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var objectID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = withUnsafeMutablePointer(to: &pidValue) { pointer in
        AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address,
            UInt32(MemoryLayout<pid_t>.size), pointer, &size, &objectID)
    }
    guard status == noErr, objectID != AudioObjectID(kAudioObjectUnknown) else {
        return nil
    }
    return objectID
}

struct AudioProcess {
    let objectID: AudioObjectID
    let pid: pid_t
    let bundle: String
}

func allAudioProcesses() -> [AudioProcess] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else {
        return []
    }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    guard count > 0 else { return [] }
    var objects = [AudioObjectID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &objects) == noErr else {
        return []
    }

    var result: [AudioProcess] = []
    for object in objects {
        var bundle = ""
        var bundleAddress = AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyBundleID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var bundleSize = UInt32(MemoryLayout<CFString?>.size)
        var bundleRef: CFString? = nil
        let bundleStatus = withUnsafeMutablePointer(to: &bundleRef) { pointer in
            AudioObjectGetPropertyData(object, &bundleAddress, 0, nil, &bundleSize, pointer)
        }
        if bundleStatus == noErr, let value = bundleRef {
            bundle = value as String
        }

        var pidAddress = AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyPID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var pid: pid_t = 0
        var pidSize = UInt32(MemoryLayout<pid_t>.size)
        guard AudioObjectGetPropertyData(object, &pidAddress, 0, nil, &pidSize, &pid) == noErr else {
            continue
        }
        result.append(AudioProcess(objectID: object, pid: pid, bundle: bundle))
    }
    return result
}

func executableName(for pid: pid_t) -> String {
    var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    let length = proc_pidpath(pid, &buffer, UInt32(buffer.count))
    guard length > 0 else { return "" }
    return URL(fileURLWithPath: String(cString: buffer)).lastPathComponent
}

let arguments = Array(CommandLine.arguments.dropFirst())
var wantedProcesses: [String] = []
var listOnly = false
var seconds: Double = 0

var index = 0
while index < arguments.count {
    switch arguments[index] {
    case "--list":
        listOnly = true
    case "--process", "-p":
        index += 1
        guard index < arguments.count else { fail("--process needs a name") }
        wantedProcesses.append(arguments[index])
    case "--seconds":
        index += 1
        guard index < arguments.count, let value = Double(arguments[index]) else {
            fail("--seconds needs a number")
        }
        seconds = value
    case "--help", "-h":
        fputs("""
        usage: wiz-tap [--process NAME]... [--list] [--seconds N]
          streams raw float32 mono audio on stdout, "RATE <hz>" on stderr

        """, stderr)
        exit(0)
    default:
        fail("unknown option '\(arguments[index])'")
    }
    index += 1
}

let processes = allAudioProcesses()

if listOnly {
    for process in processes {
        print("\(process.objectID)\t\(process.pid)\t\(executableName(for: process.pid))\t\(process.bundle)")
    }
    exit(0)
}

// Resolve the requested apps to their CoreAudio process objects.
var tapProcesses: [AudioObjectID] = []
if wantedProcesses.isEmpty {
    if processes.isEmpty {
        fputs("wiz-tap: nothing is producing audio right now\n", stderr)
    }
} else {
    for name in wantedProcesses {
        let matched = processes.filter { executableName(for: $0.pid) == name }
        if matched.isEmpty {
            fail("no audio process named '\(name)' (is it playing? try --list)")
        }
        tapProcesses.append(contentsOf: matched.map(\.objectID))
        fputs("wiz-tap: tapping \(name) as \(matched.map(\.objectID))\n", stderr)
    }
}

let description: CATapDescription
if tapProcesses.isEmpty {
    description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
} else if tapProcesses.count == 1 {
    description = CATapDescription(monoMixdownOfProcesses: tapProcesses)
} else {
    description = CATapDescription(stereoMixdownOfProcesses: tapProcesses)
}
description.isMixdown = true
description.isPrivate = true
description.muteBehavior = .unmuted

var tapID = AudioObjectID(kAudioObjectUnknown)
var status = AudioHardwareCreateProcessTap(description, &tapID)
guard status == noErr else { fail("AudioHardwareCreateProcessTap", status) }

var tapUID = "" as CFString
var uidSize = UInt32(MemoryLayout<CFString>.stride)
var uidAddress = AudioObjectPropertyAddress(
    mSelector: kAudioTapPropertyUID,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
status = withUnsafeMutablePointer(to: &tapUID) { pointer in
    AudioObjectGetPropertyData(tapID, &uidAddress, 0, nil, &uidSize, pointer)
}
guard status == noErr else { fail("read tap UID", status) }

let aggregateDescription: [String: Any] = [
    kAudioAggregateDeviceNameKey: "wiz-tap",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: tapUID]],
]
var aggregateID = AudioObjectID(kAudioObjectUnknown)
status = AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggregateID)
guard status == noErr else { fail("AudioHardwareCreateAggregateDevice", status) }

let ioProc: AudioDeviceIOProc = { _, _, inInputData, _, _, _, _ in
    let list = UnsafeMutableAudioBufferListPointer(
        UnsafeMutablePointer(mutating: inInputData))
    gBufferCount = Int32(list.count)
    guard let buffer = list.first, let data = buffer.mData else { return noErr }
    gChannels = buffer.mNumberChannels
    let byteCount = Int(buffer.mDataByteSize)
    if byteCount <= 0 { return noErr }
    let written = write(STDOUT_FILENO, data, byteCount)
    if written != byteCount { gDropCount &+= 1 }
    return noErr
}

var ioProcID: AudioDeviceIOProcID?
status = AudioDeviceCreateIOProcID(aggregateID, ioProc, nil, &ioProcID)
guard status == noErr, let validIOProcID = ioProcID else {
    fail("AudioDeviceCreateIOProcID", status)
}

var nominalRate: Float64 = 0
var rateSize = UInt32(MemoryLayout<Float64>.size)
var rateAddress = AudioObjectPropertyAddress(
    mSelector: kAudioDevicePropertyNominalSampleRate,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
AudioObjectGetPropertyData(aggregateID, &rateAddress, 0, nil, &rateSize, &nominalRate)
guard nominalRate > 0 else { fail("could not read the aggregate sample rate") }

// The tap can still deliver several interleaved channels even when it is asked
// for a mono mixdown, so resolve the real channel count and tell the reader.
var channelCount: UInt32 = 1
var streamSize: UInt32 = 0
var streamAddress = AudioObjectPropertyAddress(
    mSelector: kAudioDevicePropertyStreamConfiguration,
    mScope: kAudioObjectPropertyScopeInput,
    mElement: kAudioObjectPropertyElementMain)
if AudioObjectGetPropertyDataSize(aggregateID, &streamAddress, 0, nil, &streamSize) == noErr,
   streamSize > 0 {
    let layout = UnsafeMutableRawPointer.allocate(
        byteCount: Int(streamSize), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { layout.deallocate() }
    if AudioObjectGetPropertyData(aggregateID, &streamAddress, 0, nil, &streamSize, layout) == noErr {
        let buffers = UnsafeMutableAudioBufferListPointer(
            layout.assumingMemoryBound(to: AudioBufferList.self))
        let channels = buffers.reduce(UInt32(0)) { $0 + $1.mNumberChannels }
        if channels > 0 { channelCount = channels }
        fputs("wiz-tap: \(channels) channel(s), \(buffers.count) buffer(s)\n", stderr)
    }
}

// Non-blocking stdout: a stalled reader drops audio rather than glitching the
// real-time thread that CoreAudio delivers samples on.
_ = fcntl(STDOUT_FILENO, F_SETFL, O_NONBLOCK)

// Announce the stream format only once the tap is live, so the reader can size
// its FFT and de-interleave correctly.
fputs("FORMAT \(Int(nominalRate)) \(channelCount)\n", stderr)
fflush(stderr)

status = AudioDeviceStart(aggregateID, validIOProcID)
guard status == noErr else { fail("AudioDeviceStart", status) }

let deadline = seconds > 0 ? Date().addingTimeInterval(seconds) : Date.distantFuture
while Date() < deadline {
    // Keep the run loop drained so signals are delivered promptly.
    RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.25))
}

AudioDeviceStop(aggregateID, validIOProcID)
AudioDeviceDestroyIOProcID(aggregateID, validIOProcID)
AudioHardwareDestroyAggregateDevice(aggregateID)
AudioHardwareDestroyProcessTap(tapID)
fflush(stdout)
fputs("wiz-tap: dropped \(gDropCount) buffer(s)\n", stderr)
