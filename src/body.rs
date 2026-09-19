use crate::chunks::MAX_FILE_BYTES;
use anyhow::{Context, Result, bail, ensure};
use std::cell::RefCell;

pub(crate) struct Storage {
    compressor: RefCell<zstd::bulk::Compressor<'static>>,
}

impl Storage {
    pub fn new() -> Result<Self> {
        let mut compressor = zstd::bulk::Compressor::new(1)?;
        compressor.include_checksum(true)?;
        Ok(Self {
            compressor: RefCell::new(compressor),
        })
    }

    pub fn encode(&self, text: &str) -> Result<(Vec<u8>, i64)> {
        ensure!(
            text.len() <= MAX_FILE_BYTES,
            "Source chunk exceeds its byte limit"
        );
        if text.len() >= 256 {
            let compressed = self.compressor.borrow_mut().compress(text.as_bytes())?;
            if compressed.len() < text.len() {
                return Ok((compressed, 1));
            }
        }
        Ok((text.as_bytes().to_vec(), 0))
    }
}

pub(crate) fn decode(data: &[u8], codec: i64, raw_bytes: i64) -> Result<String> {
    ensure!(
        (0..=MAX_FILE_BYTES as i64).contains(&raw_bytes) && data.len() <= MAX_FILE_BYTES,
        "Cached source chunk exceeds its byte limit"
    );
    let expected = raw_bytes as usize;
    let bytes = match codec {
        0 => {
            ensure!(data.len() == expected, "Invalid raw source chunk length");
            return Ok(std::str::from_utf8(data)
                .context("Invalid cached source UTF-8")?
                .to_owned());
        }
        1 => {
            ensure!(
                zstd::zstd_safe::get_frame_content_size(data).ok() == Some(Some(expected as u64)),
                "Invalid compressed source chunk length"
            );
            ensure!(
                zstd::zstd_safe::find_frame_compressed_size(data).ok() == Some(data.len()),
                "Invalid compressed source frame"
            );
            zstd::bulk::decompress(data, expected).context("Cannot decompress cached source")?
        }
        _ => bail!("Unknown cached source codec {codec}"),
    };
    ensure!(
        bytes.len() == expected,
        "Invalid decoded source chunk length"
    );
    String::from_utf8(bytes).context("Invalid cached source UTF-8")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_unicode_and_keep_short_values_raw() {
        let storage = Storage::new().unwrap();
        for text in ["", "short", &"fn café() { return 東京; }\n".repeat(200)] {
            let (data, codec) = storage.encode(text).unwrap();
            assert_eq!(decode(&data, codec, text.len() as i64).unwrap(), text);
            if text.len() < 256 {
                assert_eq!(codec, 0);
            } else {
                assert_eq!(codec, 1);
                assert!(data.len() < text.len());
            }
        }
    }

    #[test]
    fn malformed_oversized_and_unknown_values_fail() {
        let text = "compressible source\n".repeat(200);
        let (mut data, _) = Storage::new().unwrap().encode(&text).unwrap();
        assert!(decode(&data, 1, MAX_FILE_BYTES as i64 + 1).is_err());
        assert!(decode(&data, 1, text.len() as i64 - 1).is_err());
        assert!(decode(&data, 7, text.len() as i64).is_err());
        assert!(decode(&data[..data.len() - 1], 1, text.len() as i64).is_err());
        let mut trailing = data.clone();
        trailing.extend_from_slice(&data);
        assert!(decode(&trailing, 1, text.len() as i64).is_err());
        data[10] ^= 1;
        assert!(decode(&data, 1, text.len() as i64).is_err());
        assert!(decode(&[255], 0, 1).is_err());
        assert!(decode(b"a", 0, 2).is_err());
        assert!(
            Storage::new()
                .unwrap()
                .encode(&"a".repeat(MAX_FILE_BYTES + 1))
                .is_err()
        );
    }
}
