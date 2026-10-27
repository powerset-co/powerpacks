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

// Window metadata is available before Accessibility is granted. Never control Settings itself.
fn settings_bounds() -> Option<core_graphics::geometry::CGRect> {
    use core_foundation::{
        base::{CFType, TCFType},
        dictionary::CFDictionary,
        number::CFNumber,
        string::CFString,
    };
    use core_graphics::{
        geometry::CGRect,
        window::{
            copy_window_info, kCGNullWindowID, kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        },
    };
    use objc2_app_kit::NSRunningApplication;

    let apps = NSRunningApplication::runningApplicationsWithBundleIdentifier(&NSString::from_str(
        "com.apple.systempreferences",
    ));
    let pid = apps.firstObject()?.processIdentifier() as i64;
    let windows = copy_window_info(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )?;
    for value in windows.iter() {
        let value = unsafe { CFType::wrap_under_get_rule(*value) };
        let Some(dict) = value.downcast::<CFDictionary>() else {
            continue;
        };
        let row: CFDictionary<CFString, CFType> =
            unsafe { TCFType::wrap_under_get_rule(dict.as_concrete_TypeRef()) };
        let number = |key: &str| {
            row.find(CFString::new(key))?
                .downcast::<CFNumber>()?
                .to_i64()
        };
        if number("kCGWindowOwnerPID") != Some(pid) || number("kCGWindowLayer") != Some(0) {
            continue;
        }
        let bounds = row
            .find(CFString::new("kCGWindowBounds"))?
            .downcast::<CFDictionary>()?;
        let rect = CGRect::from_dict_representation(&bounds)?;
        if rect.size.width > 0.0 && rect.size.height > 0.0 {
            return Some(rect);
        }
    }
    None
}

fn panel_origin(settings: NSRect, visible: NSRect) -> NSPoint {
    let (width, height) = super::HELPER;
    let sidebar = if settings.size.width > 430.0 {
        215.0
    } else {
        0.0
    };
    let x = settings.origin.x + sidebar + (settings.size.width - sidebar - width) / 2.0;
    NSPoint::new(
        x.max(visible.origin.x)
            .min((visible.origin.x + visible.size.width - width).max(visible.origin.x))
            .round(),
        (settings.origin.y - height - 12.0)
            .max(visible.origin.y + 24.0)
            .round(),
    )
}

pub unsafe fn follow_settings(window: *mut NSWindow) {
    use objc2_app_kit::{NSApplication, NSApplicationActivationOptions, NSRunningApplication, NSScreen};
    use objc2_foundation::NSIntersectsRect;

    let window = &*window;
    let Some(bounds) = settings_bounds() else {
        window.orderOut(None);
        return;
    };
    let main = MainThreadMarker::new().expect("Webview callback runs on the main thread");
    let screens = NSScreen::screens(main);
    let Some(primary) = screens.firstObject() else {
        return;
    };
    // Quartz uses a top-left origin; AppKit uses the primary display's bottom-left origin.
    let settings = NSRect::new(
        NSPoint::new(
            bounds.origin.x,
            primary.frame().size.height - bounds.origin.y - bounds.size.height,
        ),
        NSSize::new(bounds.size.width, bounds.size.height),
    );
    let screen = screens
        .iter()
        .find(|screen| NSIntersectsRect(settings, screen.frame()))
        .unwrap_or(primary);
    let origin = panel_origin(settings, screen.visibleFrame());
    if window.frame().origin != origin {
        window.setFrameOrigin(origin);
    }
    if !window.isVisible() {
        window.orderFrontRegardless();
        // The first time the helper shows, System Settings comes to the front: Powerpacks' own window
        // would otherwise sit over the list the person is about to drag into. macOS 14 lets an app
        // hand its activation to another; the activate call is for the versions before it.
        let settings = NSRunningApplication::runningApplicationsWithBundleIdentifier(
            &NSString::from_str("com.apple.systempreferences"),
        );
        if let Some(settings) = settings.firstObject() {
            NSApplication::sharedApplication(main).yieldActivationToApplication(&settings);
            settings.activateWithOptions(NSApplicationActivationOptions::ActivateAllWindows);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn panel_follows_the_settings_content_column_and_stays_on_screen() {
        let visible = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(1440.0, 875.0));
        let settings = NSRect::new(NSPoint::new(300.0, 300.0), NSSize::new(700.0, 550.0));
        assert_eq!(panel_origin(settings, visible), NSPoint::new(478.0, 144.0));
        let moved = NSRect::new(NSPoint::new(400.0, 100.0), settings.size);
        assert_eq!(panel_origin(moved, visible), NSPoint::new(578.0, 24.0));
        let secondary = NSRect::new(NSPoint::new(-1440.0, 0.0), visible.size);
        let settings = NSRect::new(NSPoint::new(-1450.0, 400.0), settings.size);
        assert_eq!(
            panel_origin(settings, secondary),
            NSPoint::new(-1273.0, 244.0)
        );
    }
}
