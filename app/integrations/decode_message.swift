// Decode Apple's attributed message archive without guessing text from bytes.
import Foundation

if CommandLine.arguments.contains("--self-test") {
    let sample = NSAttributedString(string: "Synthetic text: Sunday at 9 ☀️")
    let data = NSArchiver.archivedData(withRootObject: sample)
    let decoded = NSUnarchiver.unarchiveObject(with: data)
    guard (decoded as? NSAttributedString)?.string == sample.string else { exit(1) }
    print("Attributed message decoding passed")
    exit(0)
}
let input = FileHandle.standardInput.readDataToEndOfFile()
guard let encoded = String(data: input, encoding: .utf8),
      let data = Data(base64Encoded: encoded.trimmingCharacters(in: .whitespacesAndNewlines)),
      data.count <= 1_048_576 else { exit(2) }
guard let text = NSUnarchiver.unarchiveObject(with: data) as? NSAttributedString else { exit(3) }
FileHandle.standardOutput.write(Data(text.string.utf8))
