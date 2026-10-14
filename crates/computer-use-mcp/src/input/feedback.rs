//! Optional cursor-position feedback for animated portal pointer motion.
//! Qt reports position only; generated input continues through the approved EIS route.
use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::PathBuf,
    time::Duration,
};

use serde_json::Value;

use super::coordinates::EisRegion;

pub fn smooth_pointer_enabled() -> bool {
    std::env::var("COMPUTER_USE_MCP_SMOOTH_POINTER").as_deref() == Ok("1")
}

pub async fn cursor_position(region: &EisRegion) -> Result<(f64, f64), String> {
    let sample = tokio::task::spawn_blocking(read_cursor)
        .await
        .map_err(|error| format!("cursor feedback worker failed: {error}"))??;
    validate_cursor(region, &sample)
}

fn read_cursor() -> Result<Value, String> {
    let root = std::env::var_os("XDG_RUNTIME_DIR").ok_or("XDG_RUNTIME_DIR is unavailable")?;
    let path = PathBuf::from(root).join("codex-activity-halo/feedback.sock");
    let mut socket = UnixStream::connect(path)
        .map_err(|error| format!("cursor indicator is unavailable: {error}"))?;
    socket
        .set_read_timeout(Some(Duration::from_millis(300)))
        .map_err(|error| error.to_string())?;
    socket
        .set_write_timeout(Some(Duration::from_millis(300)))
        .map_err(|error| error.to_string())?;
    socket
        .write_all(b"{\"op\":\"cursor\"}\n")
        .map_err(|error| error.to_string())?;
    let mut reader = BufReader::new(socket);
    let mut bytes = Vec::new();
    // Bound the response even if a local peer returns no newline.
    std::io::Read::take(&mut reader, 4096)
        .read_until(b'\n', &mut bytes)
        .map_err(|error| error.to_string())?;
    if !bytes.ends_with(b"\n") {
        return Err("cursor feedback response is truncated".into());
    }
    serde_json::from_slice(&bytes).map_err(|error| error.to_string())
}

fn validate_cursor(region: &EisRegion, sample: &Value) -> Result<(f64, f64), String> {
    if region.mapping_id.as_deref() != sample.get("screen").and_then(Value::as_str)
        || region.mapping_id.is_none()
    {
        return Err("place the pointer on the selected screen before animated control".into());
    }
    let pair = |key: &str| -> Option<(i64, i64)> {
        let fields = sample.get(key)?.as_array()?;
        if fields.len() != 2 {
            return None;
        }
        Some((fields[0].as_i64()?, fields[1].as_i64()?))
    };
    if pair("position") != Some((i64::from(region.position.0), i64::from(region.position.1)))
        || pair("size") != Some((i64::from(region.size.0), i64::from(region.size.1)))
    {
        return Err("cursor feedback geometry differs from the approved EIS region".into());
    }
    let x = sample.get("x").and_then(Value::as_f64).ok_or("cursor x is missing")?;
    let y = sample.get("y").and_then(Value::as_f64).ok_or("cursor y is missing")?;
    if !contains(region, (x, y)) {
        return Err("cursor feedback lies outside the selected EIS region".into());
    }
    Ok((x, y))
}

fn contains(region: &EisRegion, point: (f64, f64)) -> bool {
    let (left, top) = (f64::from(region.position.0), f64::from(region.position.1));
    point.0.is_finite() && point.1.is_finite()
        && point.0 >= left && point.0 < left + f64::from(region.size.0)
        && point.1 >= top && point.1 < top + f64::from(region.size.1)
}

pub fn trajectory(region: &EisRegion, from: (f64, f64), to: (f64, f64)) -> Result<Vec<(f64, f64)>, String> {
    if !contains(region, from) || !contains(region, to) {
        return Err("animated pointer path would leave the selected screen".into());
    }
    let distance = (to.0 - from.0).hypot(to.1 - from.1);
    if distance < 3.0 {
        return Ok(vec![to]);
    }
    let steps = ((distance / 1500.0).clamp(0.12, 0.65) * 60.0).ceil() as usize;
    Ok((1..=steps).map(|step| {
        let t = step as f64 / steps as f64;
        let eased = t * t * (3.0 - 2.0 * t);
        (from.0 + (to.0 - from.0) * eased, from.1 + (to.1 - from.1) * eased)
    }).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn region() -> EisRegion {
        EisRegion { position: (100, 0), size: (1920, 1080), mapping_id: Some("HDMI-A-1".into()) }
    }

    #[test]
    fn cursor_feedback_requires_same_output_and_geometry() {
        let mut sample = json!({"screen":"HDMI-A-1", "position":[100,0], "size":[1920,1080], "x":500,"y":400});
        assert_eq!(validate_cursor(&region(), &sample).unwrap(), (500.0, 400.0));
        sample["screen"] = json!("eDP-1");
        assert!(validate_cursor(&region(), &sample).is_err());
        sample["screen"] = json!("HDMI-A-1");
        sample["size"] = json!([960,540]);
        assert!(validate_cursor(&region(), &sample).is_err());
    }

    #[test]
    fn trajectories_stay_on_selected_screen_and_end_exactly() {
        let path = trajectory(&region(), (101.0, 2.0), (2019.0, 1079.0)).unwrap();
        assert_eq!(path.last(), Some(&(2019.0, 1079.0)));
        assert!(path.len() > 10 && path.len() <= 40);
        assert!(path.iter().all(|point| contains(&region(), *point)));
        assert!(path.windows(2).all(|pair| pair[0].0 <= pair[1].0 && pair[0].1 <= pair[1].1));
        assert!(trajectory(&region(), (99.0, 0.0), (500.0, 400.0)).is_err());
        assert!(trajectory(&region(), (500.0, 400.0), (2020.0, 400.0)).is_err());
        assert!(trajectory(&region(), (f64::NAN, 0.0), (500.0, 400.0)).is_err());
    }
}
