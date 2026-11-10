package smoothui.text
{
	import flash.utils.Dictionary;
	import flash.utils.getTimer;

	/**
	 * Stick scrolling of the game's text, at a speed in pixels per second.
	 *
	 * scroll: for TextAreaModuleCustomInput (the text of Glossary entries and of Journal > Quests), with the
	 * right stick. The game's handler moves the scroll bar by the stick's tilt, in lines, on every stick event,
	 * and those come every frame while the stick is tilted: the speed followed the frame rate. The patcher
	 * makes that handler call scroll(this, tilt) instead (see text_stick_scroll_fix.py).
	 *
	 * textAreaNavigation: for CLIK's TextArea (books), with the left stick. It scrolled a line per up/down key
	 * event, which the left stick sends at the engine's key repeat rate, whatever the tilt. The patcher makes
	 * its handleInput call textAreaNavigation(this, event) for the navigation code to act on (see
	 * book_stick_scroll_fix.py).
	 *
	 * The speed is in pixels, not lines, so (modded) bigger fonts don't scroll faster.
	 */
	public class StickScroll
	{
		// Pixels per second: SLOW_SPEED just past the dead zone, growing exponentially to FAST_SPEED at full
		// tilt, so that each bit of extra tilt multiplies the speed by the same factor (about 1.4x per tenth)
		private static const SLOW_SPEED:Number = 40;
		private static const FAST_SPEED:Number = 1400;

		// Tilts below this don't scroll
		private static const DEAD_ZONE:Number = 0.08;

		// A longer gap between stick events (ms) means the stick was at rest in between
		private static const MAX_EVENT_GAP:int = 100;

		// The game's KeyCode.PAD_LEFT_STICK_AXIS, and CLIK's NavigationCode.UP and DOWN
		private static const PAD_LEFT_STICK_AXIS:int = 1000;
		private static const UP:String = "up";
		private static const DOWN:String = "down";

		// Per scrolling object: [the time of its last stick event, the fraction of a line scrolled but not shown]
		private static var states:Dictionary = new Dictionary(true);

		public static function scroll(module:Object, tilt:Number):void
		{
			var whole:int = lines(module, module.mcTextArea.textField, tilt);
			if (whole != 0)
			{
				module.mcScrollbar.position += whole;
			}
		}

		// The navigation code TextArea.handleInput should act on for an input event (in place of its own
		// event.details.navEquivalent), scrolling textArea for the left stick's axis events. The left stick's
		// up/down key events (one line each) get none.
		public static function textAreaNavigation(textArea:Object, event:Object):String
		{
			var details:Object = event.details;
			if (details.code == PAD_LEFT_STICK_AXIS)
			{
				var whole:int = lines(textArea, textArea.textField, details.value.yvalue);
				var position:int = Math.max(1, Math.min(textArea.textField.maxScrollV, textArea.position + whole));
				// Not at the ends: setting the position plays a sound, a "can't scroll" one if it doesn't change
				if (position != textArea.position)
				{
					textArea.position = position;
				}
			}
			else if (details.fromJoystick && (details.navEquivalent == UP || details.navEquivalent == DOWN))
			{
				// Handled where the game's line step would have been
				if (details.navEquivalent == UP ? textArea.position > 1 : textArea.position < textArea.textField.maxScrollV)
				{
					event.handled = true;
				}
				return null;
			}
			return details.navEquivalent;
		}

		// The whole lines to scroll owner's textField by, for a stick event with this tilt (up is positive)
		private static function lines(owner:Object, textField:Object, tilt:Number):int
		{
			var now:int = getTimer();
			var state:Array = states[owner];
			if (state == null || now - state[0] > MAX_EVENT_GAP)
			{
				states[owner] = [now, 0];
				return 0;
			}
			var elapsed:int = now - state[0];
			state[0] = now;

			var amount:Number = (Math.abs(tilt) - DEAD_ZONE) / (1 - DEAD_ZONE);
			if (amount <= 0 || textField.numLines < 1 || textField.textHeight <= 0)
			{
				return 0;
			}
			var lineHeight:Number = textField.textHeight / textField.numLines;
			var pixels:Number = SLOW_SPEED * Math.pow(FAST_SPEED / SLOW_SPEED, Math.min(amount, 1)) * elapsed / 1000;

			// Positive tilt value = stick up, so scroll upwards (reduce position)
			var total:Number = state[1] + (tilt > 0 ? -pixels : pixels) / lineHeight;
			var whole:int = int(total);
			state[1] = total - whole;
			return whole;
		}
	}
}
