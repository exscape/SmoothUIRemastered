package smoothui.text
{
	import flash.utils.Dictionary;
	import flash.utils.getTimer;

	/**
	 * Right-stick scrolling for the game's TextAreaModuleCustomInput (the text of Glossary entries and of
	 * Journal > Quests), at a speed in pixels per second.
	 *
	 * The game's handler moves the scroll bar by the stick's tilt, in lines, on every stick event, and those
	 * come every frame while the stick is tilted: the speed followed the frame rate.
	 *
	 * The patcher makes that handler call scroll(this, tilt) instead (see text_stick_scroll_fix.py).
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

		// Per text module: [the time of its last stick event, the fraction of a line scrolled but not shown]
		private static var states:Dictionary = new Dictionary(true);

		public static function scroll(module:Object, tilt:Number):void
		{
			var now:int = getTimer();
			var state:Array = states[module];
			if (state == null || now - state[0] > MAX_EVENT_GAP)
			{
				states[module] = [now, 0];
				return;
			}
			var elapsed:int = now - state[0];
			state[0] = now;

			var amount:Number = (Math.abs(tilt) - DEAD_ZONE) / (1 - DEAD_ZONE);
			var textField:Object = module.mcTextArea.textField;
			if (amount <= 0 || textField.numLines < 1 || textField.textHeight <= 0)
			{
				return;
			}
			var lineHeight:Number = textField.textHeight / textField.numLines;
			var pixels:Number = SLOW_SPEED * Math.pow(FAST_SPEED / SLOW_SPEED, Math.min(amount, 1)) * elapsed / 1000;

			// Positive tilt value = stick up, so scroll upwards (reduce position)
			var lines:Number = state[1] + (tilt > 0 ? -pixels : pixels) / lineHeight;
			var whole:int = int(lines);
			state[1] = lines - whole;
			if (whole != 0)
			{
				module.mcScrollbar.position += whole;
			}
		}
	}
}
