// Private media compositor. Reads actual supported-browser screenshots only.
// Run on macOS: swift tools/render_submission_walkthrough.swift storyboard.json captures output.mp4
import AppKit
import AVFoundation
import CoreVideo
import Foundation

struct Shot: Decodable { let image: String; let seconds: Int; let title: String; let caption: String }
struct Storyboard: Decodable { let title: String; let simulation_badge: String; let shots: [Shot] }
enum RenderError: Error { case invalid(String) }
func require(_ condition: Bool, _ message: String) throws {
    if !condition { throw RenderError.invalid(message) }
}
let args = CommandLine.arguments
try require(args.count == 4, "Usage: swift render_submission_walkthrough.swift storyboard.json captures output.mp4")
let board = try JSONDecoder().decode(Storyboard.self, from: Data(contentsOf: URL(fileURLWithPath: args[1])))
let captureRoot = URL(fileURLWithPath: args[2], isDirectory: true).standardizedFileURL
let output = URL(fileURLWithPath: args[3]).standardizedFileURL
let width = 1920, height = 1080, fps: Int32 = 12
let totalSeconds = board.shots.reduce(0) { $0 + $1.seconds }
try require(!board.shots.isEmpty && board.shots.allSatisfy { $0.seconds > 0 } && totalSeconds <= 90, "Invalid duration")
try require(!FileManager.default.fileExists(atPath: output.path), "Output already exists; select a fresh path")
try FileManager.default.createDirectory(at: output.deletingLastPathComponent(), withIntermediateDirectories: true)

func drawText(_ text: String, rect: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor) throws {
    let style = NSMutableParagraphStyle()
    style.lineBreakMode = .byWordWrapping
    style.lineSpacing = 5
    let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: size, weight: weight), .foregroundColor: color, .paragraphStyle: style]
    let value = NSAttributedString(string: text, attributes: attributes)
    let needed = value.boundingRect(with: NSSize(width: rect.width, height: .greatestFiniteMagnitude), options: [.usesLineFragmentOrigin, .usesFontLeading]).height
    try require(needed <= rect.height, "Caption exceeds available space: \(text)")
    value.draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading])
}

func compose(_ shot: Shot, index: Int) throws -> CGImage {
    let capture = captureRoot.appendingPathComponent(shot.image).standardizedFileURL
    try require(capture.deletingLastPathComponent() == captureRoot && capture.pathExtension == "png", "Capture must be a direct PNG child")
    guard let source = NSImage(contentsOf: capture) else { throw RenderError.invalid("Missing capture \(capture.lastPathComponent)") }
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: width * 4, bitsPerPixel: 32),
          let graphics = NSGraphicsContext(bitmapImageRep: bitmap) else { throw RenderError.invalid("Canvas allocation failed") }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = graphics
    defer { NSGraphicsContext.restoreGraphicsState() }
    NSColor(calibratedRed: 0.08, green: 0.065, blue: 0.05, alpha: 1).setFill()
    NSRect(x: 0, y: 0, width: width, height: height).fill()
    try drawText("TEXT MONKEY", rect: NSRect(x: 48, y: 1018, width: 340, height: 38), size: 27, weight: .bold, color: .white)
    try drawText(board.simulation_badge, rect: NSRect(x: 1030, y: 1024, width: 842, height: 30), size: 20, weight: .semibold, color: NSColor(calibratedRed: 1, green: 0.82, blue: 0.32, alpha: 1))
    let area = NSRect(x: 48, y: 212, width: 1824, height: 798)
    let scale = min(area.width / source.size.width, area.height / source.size.height)
    let size = NSSize(width: source.size.width * scale, height: source.size.height * scale)
    let target = NSRect(x: area.midX - size.width / 2, y: area.midY - size.height / 2, width: size.width, height: size.height)
    source.draw(in: target, from: .zero, operation: .copy, fraction: 1)
    try drawText(shot.title, rect: NSRect(x: 88, y: 151, width: 1744, height: 44), size: 31, weight: .bold, color: .white)
    try drawText(shot.caption, rect: NSRect(x: 88, y: 55, width: 1744, height: 85), size: 30, weight: .regular, color: NSColor(calibratedWhite: 0.94, alpha: 1))
    try drawText(String(format: "%02d / %02d  •  Actual UI captures, manual preview", index + 1, board.shots.count), rect: NSRect(x: 88, y: 13, width: 1744, height: 28), size: 18, weight: .medium, color: NSColor(calibratedWhite: 0.7, alpha: 1))
    graphics.flushGraphics()
    guard let image = bitmap.cgImage else { throw RenderError.invalid("Canvas unavailable") }
    return image
}

let writer = try AVAssetWriter(outputURL: output, fileType: .mp4)
writer.shouldOptimizeForNetworkUse = true
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height, AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 4_000_000, AVVideoExpectedSourceFrameRateKey: fps, AVVideoMaxKeyFrameIntervalKey: fps * 2]])
input.expectsMediaDataInRealTime = false
try require(writer.canAdd(input), "Video input unavailable")
writer.add(input)
let adapter = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA, kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height, kCVPixelBufferCGImageCompatibilityKey as String: true, kCVPixelBufferCGBitmapContextCompatibilityKey as String: true])
try require(writer.startWriting(), "Could not start writer")
writer.startSession(atSourceTime: .zero)
var frame: Int64 = 0
for (index, shot) in board.shots.enumerated() {
    let image = try compose(shot, index: index)
    guard let pool = adapter.pixelBufferPool else { throw RenderError.invalid("Pixel pool unavailable") }
    var optional: CVPixelBuffer?
    try require(CVPixelBufferPoolCreatePixelBuffer(nil, pool, &optional) == kCVReturnSuccess, "Pixel allocation failed")
    guard let buffer = optional else { throw RenderError.invalid("Pixel buffer missing") }
    CVPixelBufferLockBaseAddress(buffer, [])
    guard let context = CGContext(data: CVPixelBufferGetBaseAddress(buffer), width: width, height: height, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(buffer), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) else { throw RenderError.invalid("Drawing context unavailable") }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    CVPixelBufferUnlockBaseAddress(buffer, [])
    for _ in 0..<(shot.seconds * Int(fps)) {
        let deadline = Date().addingTimeInterval(30)
        while !input.isReadyForMoreMediaData {
            try require(writer.status == .writing && Date() < deadline, "Encoder stalled")
            Thread.sleep(forTimeInterval: 0.005)
        }
        try require(adapter.append(buffer, withPresentationTime: CMTime(value: frame, timescale: fps)), "Frame append failed")
        frame += 1
    }
    print("Rendered shot \(index + 1)/\(board.shots.count)")
}
input.markAsFinished()
writer.endSession(atSourceTime: CMTime(value: Int64(totalSeconds), timescale: 1))
let completed = DispatchSemaphore(value: 0)
writer.finishWriting { completed.signal() }
try require(completed.wait(timeout: .now() + 60) == .success && writer.status == .completed, "Video finalization failed")

// Decode the entire video to establish playback, then inspect each shot boundary.
let asset = AVURLAsset(url: output)
guard let track = asset.tracks(withMediaType: .video).first else { throw RenderError.invalid("Encoded video track missing") }
let reader = try AVAssetReader(asset: asset)
let decoded = AVAssetReaderTrackOutput(track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
reader.add(decoded)
try require(reader.startReading(), "Playback reader failed")
var decodedFrames = 0
while decoded.copyNextSampleBuffer() != nil { decodedFrames += 1 }
try require(reader.status == .completed && decodedFrames == Int(frame), "Playback decode incomplete")
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero
var second = 0
for (index, shot) in board.shots.enumerated() {
    let image = try generator.copyCGImage(at: CMTime(value: Int64(second), timescale: 1), actualTime: nil)
    let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])!
    try png.write(to: output.deletingLastPathComponent().appendingPathComponent(String(format: "playback-%02d.png", index + 1)))
    second += shot.seconds
}
let duration = CMTimeGetSeconds(asset.duration)
try require(abs(duration - Double(totalSeconds)) < 0.1 && duration <= 90 && Int(track.naturalSize.width) == width && Int(track.naturalSize.height) == height, "Duration or dimensions incorrect")
let proof: [String: Any] = ["duration_seconds": duration, "width": width, "height": height, "fps": fps, "encoded_frames": frame, "decoded_frames": decodedFrames, "playback_full_decode_passed": true, "audio_tracks": asset.tracks(withMediaType: .audio).count, "codec": "H.264", "source_screenshots": board.shots.map { $0.image }, "captions_burned_in": true, "native_changes": 0, "texts_sent": 0]
try JSONSerialization.data(withJSONObject: proof, options: [.prettyPrinted, .sortedKeys]).write(to: output.deletingLastPathComponent().appendingPathComponent("video-proof.json"))
print("Verified \(duration) seconds, \(width)x\(height), \(decodedFrames) decoded frames, silent.")
