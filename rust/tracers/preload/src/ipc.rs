pub use tracer_schema::NativeTraceEvent as TraceEvent;

#[cfg(test)]
mod tests {
    use super::TraceEvent;

    #[test]
    fn trace_event_round_trip_preserves_thread_id() {
        let event = TraceEvent::Write {
            pid: 42,
            thread_id: 99,
            path: "/tmp/native-thread.txt".to_string(),
        };

        let payload = rmp_serde::to_vec_named(&event).expect("serialize trace event");
        let decoded: TraceEvent = rmp_serde::from_slice(&payload).expect("decode trace event");

        assert_eq!(decoded, event);
    }
}
