//! Menyajikan gui.html dan aset statis yang menyertainya.
//!
//! Tidak ada build step dan tidak ada CDN: satu berkas HTML dibaca dari disk setiap kali
//! diminta, dengan fallback ke salinan yang ikut ter-compile kalau berkasnya hilang.
//! Header anti-cache di sini bukan hiasan — tanpa itu WebKit boleh menyajikan hasil lama
//! dan tombol "Refresh UI" terlihat berhasil padahal yang termuat JavaScript lama.
use axum::extract::Path;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use std::fs;

use crate::paths::{assets_dir, gui_path, station_path};

pub(crate) async fn serve_gui() -> Response {
    let html = fs::read_to_string(gui_path())
        .or_else(|_| Ok::<String, std::io::Error>(include_str!("../gui.html").to_string()))
        .unwrap_or_else(|_| "<h1>gui.html tidak ditemukan</h1>".to_string());
    Response::builder()
        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
        .header(header::CACHE_CONTROL, "no-store, must-revalidate")
        .body(axum::body::Body::from(html))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

pub(crate) async fn serve_station() -> Response {
    match fs::read_to_string(station_path()) {
        Ok(html) => Response::builder()
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            .header(header::CACHE_CONTROL, "no-store")
            .body(axum::body::Body::from(html))
            .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response()),
        Err(_) => (StatusCode::NOT_FOUND, "station.html belum terpasang").into_response(),
    }
}

pub(crate) async fn serve_asset(Path(rel): Path<String>) -> Response {
    if rel.contains("..") {
        return StatusCode::BAD_REQUEST.into_response();
    }
    let full = assets_dir().join(&rel);
    match fs::read(&full) {
        Ok(bytes) => {
            let ctype = match full.extension().and_then(|e| e.to_str()).unwrap_or("") {
                "js" => "text/javascript; charset=utf-8",
                "css" => "text/css; charset=utf-8",
                "woff2" => "font/woff2",
                "png" => "image/png",
                "svg" => "image/svg+xml",
                _ => "application/octet-stream",
            };
            Response::builder()
                .header(header::CONTENT_TYPE, ctype)
                .header(header::CACHE_CONTROL, "public, max-age=3600")
                .body(axum::body::Body::from(bytes))
                .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
        }
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}
