use std::path::Path;

use objc2::{
    define_class, msg_send, rc::Retained, runtime::ProtocolObject, AnyThread, MainThreadOnly,
};
use objc2_app_kit::{
    NSDragOperation, NSDraggingContext, NSDraggingItem, NSDraggingSession, NSDraggingSource,
    NSEvent, NSEventType, NSView, NSWindow, NSWorkspace,
};
use objc2_foundation::{
    MainThreadMarker, NSArray, NSObjectProtocol, NSPoint, NSRect, NSSize, NSString, NSURL,
};

define_class!(
    #[unsafe(super = NSView)]
    #[thread_kind = MainThreadOnly]
    struct PermissionDrag;

    unsafe impl NSObjectProtocol for PermissionDrag {}

    unsafe impl NSDraggingSource for PermissionDrag {
        #[unsafe(method(draggingSession:sourceOperationMaskForDraggingContext:))]
        fn operation(
            &self,
            _session: &NSDraggingSession,
            _context: NSDraggingContext,
        ) -> NSDragOperation {
            NSDragOperation::Copy
        }

        #[unsafe(method(draggingSession:endedAtPoint:operation:))]
        fn ended(
            &self,
            _session: &NSDraggingSession,
            _point: NSPoint,
            _operation: NSDragOperation,
        ) {
            self.removeFromSuperview();
        }
    }
);

pub unsafe fn drag(window: *mut NSWindow, path: &Path) -> Result<(), String> {
    let main = MainThreadMarker::new().ok_or("Drag must start on the main thread.")?;
    let window = &*window;
    if NSEvent::pressedMouseButtons() & 1 == 0 {
        return Err("Hold the icon while dragging.".into());
    }
    let event = NSEvent::mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure(
        NSEventType::LeftMouseDragged,
        window.mouseLocationOutsideOfEventStream(),
        NSEvent::modifierFlags_class(),
        0.0,
        window.windowNumber(),
        None,
        0,
        1,
        1.0,
    ).ok_or("Could not start the file drag.")?;
    let view = window.contentView().ok_or("The helper has no view.")?;
    let path = NSString::from_str(path.to_str().ok_or("The app path is not UTF-8.")?);
    let url = NSURL::fileURLWithPath_isDirectory(&path, true);
    let item = NSDraggingItem::initWithPasteboardWriter(
        NSDraggingItem::alloc(),
        ProtocolObject::from_ref(&*url),
    );
    let icon = NSWorkspace::sharedWorkspace().iconForFile(&path);
    icon.setSize(NSSize::new(64.0, 64.0));
    let point = view.convertPoint_fromView(event.locationInWindow(), None);
    item.setDraggingFrame_contents(
        NSRect::new(
            NSPoint::new(point.x - 32.0, point.y - 32.0),
            NSSize::new(64.0, 64.0),
        ),
        Some(&icon),
    );
    let source: Retained<PermissionDrag> =
        msg_send![PermissionDrag::alloc(main), initWithFrame: NSRect::ZERO];
    view.addSubview(&source);
    view.beginDraggingSessionWithItems_event_source(
        &NSArray::from_retained_slice(&[item]),
        &event,
        ProtocolObject::from_ref(&*source),
    );
    Ok(())
}
