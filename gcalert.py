#!/usr/bin/python
# vim: ai expandtab
#
# This script will periodically check your Google Calendar and display an alarm
# when that's set for the event. It reads ALL your calendars automatically.
#
# Only 'popup' alarms will result in what's essentially a popup. This is a feature :)
#
# Requires: python-notify python-gdata python-dateutil notification-daemon
#
# Home: http://github.com/raas/gcalert
#
# ----------------------------------------------------------------------------
# 
# Copyright 2009 Andras Horvath (andras.horvath nospamat gmailcom) This
# program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your
# option) any later version.
# 
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
# 
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
# 
# ----------------------------------------------------------------------------
#
# TODO:
# - warn for unsecure permissions of the password/secret file
# - option for strftime in alarms
# - use some sort of proper logging with log levels etc
# - options for selecting which calendars to alert (currently: all of them)
# - snooze buttons; this requires a gtk.main() thread and that's not trivial
# - testing (as in, unit testing), after having a main()
# - multi-language support

import getopt
import sys
import os
import time
import urllib
import thread
import signal

# dependencies below come from separate packages, the rest (above) is in the
# standard library so those are expected to work :)
try:
    # google calendar stuff
    import gdata.calendar.service 
    import gdata.service
    import gdata.calendar
    # libnotify handler
    import pynotify
    # magical date parser and timezone handler
    import dateutil.tz 
    import dateutil.parser 
except ImportError as e:
    print "Dependency was not found! %s" % e
    print "(Try: sudo apt-get install python-notify python-gdata python-dateutil notification-daemon)"
    sys.exit(1)

# -------------------------------------------------------------------------------------------

myversion = '1.3'

# -------------------------------------------------------------------------------------------
# default values for parameters

secrets_file = os.path.join(os.environ["HOME"],".gcalert_secret")
alarm_sleeptime = 30 # seconds between waking up to check alarm list
query_sleeptime = 180 # seconds between querying Google 
lookahead_days = 3 # look this many days in the future
debug_flag = False
login_retry_sleeptime = 300 # seconds between reconnects in case of errors
threads_offset = 5 # this many seconds offset between the two threads' runs
# -------------------------------------------------------------------------------------------
# end of user-changeable stuff here
# -------------------------------------------------------------------------------------------

events=[] # all events seen so far that are yet to start
events_lock=thread.allocate_lock() # hold to access events[]
alarmed_events = [] # events (occurences etc) already alarmed
connected = False # google connection is disconnected

class GcEvent(object):
    """
    One event occurence (actually, one alarm for an event)
    """
    def __init__(self, title, where, start_string, end_string, minutes):
        """
        title: event title text
        where: where is the event (or empty string)
        start_string: event start time as string
        end_string: event end time as string
        minutes: how many minutes before the start is the alarm to go off
        """
        self.title=title
        self.where=where
        self.start=dateutil.parser.parse(start_string)
        self.end=dateutil.parser.parse(end_string)
        # Google sometimes does not supply timezones
        # (for events that last more than a day and have no time set, apparently)
        # python can't compare two dates if only one has TZ info
        # this might screw us at, say, if DST changes between when we get the event and its alarm
        if not self.start.tzname():
            self.start=self.start.replace(tzinfo=dateutil.tz.tzlocal())
        if not self.end.tzname():
            self.end=self.end.replace(tzinfo=dateutil.tz.tzlocal())
        self.minutes=minutes

    def get_starttime_str(self):
        """Start time in local timezone, as a preformatted string"""
        return self.start.astimezone(dateutil.tz.tzlocal()).strftime('%Y-%m-%d  %H:%M')

    def get_starttime_unix(self):
        """Start time in unix time"""
        return int(self.start.astimezone(dateutil.tz.tzlocal()).strftime('%s'))

    def get_alarm_time_unix(self):
        """Alarm time in unix time"""
        return self.starttime_unix-60*int(self.minutes)

    starttime_str=property(fget=get_starttime_str) 
    starttime_unix=property(fget=get_starttime_unix) 
    alarm_time_unix=property(fget=get_alarm_time_unix) 

    def alarm(self):
        """Show the alarm box for one event/recurrence"""
        message( " ***** ALARM ALARM ALARM: %s (%s) %s ****  " % ( self.title, self.where, self.starttime_str )  )
        if self.where:
            a=pynotify.Notification( self.title, "<b>Starting:</b> %s\n<b>Where:</b> %s" % (self.starttime_str, self.where), 'gtk-dialog-info')
        else:
            a=pynotify.Notification( self.title, "<b>Starting:</b> %s" % self.starttime_str, 'gtk-dialog-info')
        # let the alarm stay until it's closed by hand (acknowledged)
        a.set_timeout(pynotify.EXPIRES_NEVER)
        if not a.show():
            message( "Failed to send alarm notification!" )


# ----------------------------

def message(s):
    """Print one message 's' and flush the buffer; useful when redirected to a file"""
    print "%s gcalert.py: %s" % ( time.asctime(), s)
    sys.stdout.flush()

# ----------------------------

def debug(s):
    """Print debug message 's' if the debug_flag is set (running with -d option)"""
    if (debug_flag):
        message("DEBUG: %s" % s)

# ----------------------------

# signal handlers are easier than wrapping the whole show
# into one giant try/except looking for KeyboardInterrupt
# besides we have two threads to shut down
def stopthismadness(signl, frme):
    """Hook up signal handler for ^C"""
    message("shutting down on SIGINT")
    sys.exit(0)

# ----------------------------
#
def get_user_calendars(calendarservice):
    """
    Get the list of 'magic strings' used to identify each calendar
    calendarservice: Calendar Service as returned by get_calendar_service()
    returns: list(username) that each can be used in CalendarEventQuery()
    """
    try:
        feed = calendarservice.GetAllCalendarsFeed()
    # in there is the full feed URL and we need the last part (=='username')
    except Exception as error: # FIXME clearer
        debug( "Google connection lost: %s" % error )
        try:
            message( "Google connection lost (%s %s), will re-connect" % (error['status'], error['reason']) )
        except Exception:
            message( "Google connection lost with unknown error, will re-connect: %s " % error )
        return None
    return map(lambda x: urllib.unquote(x.id.text.split('/')[-1]), feed.entry) 

# ----------------------------
def date_range_query(cs, start_date='2007-01-01', end_date='2007-07-01'):
    """
    Get a list of events happening between the given dates in all calendars the user has
    cs: Calendar Service as returned by get_calendar_service()
    returns: (success, list of events)
    
    Each reminder occurence creates a new event (new GcEvent object)
    """
    
    el = [] # event occurence list
    for username in get_user_calendars(cs):
        try:
            query = gdata.calendar.service.CalendarEventQuery(username, 'private', 'full')
            query.start_min = start_date
            query.start_max = end_date 
            feed = cs.CalendarQuery(query)
        except Exception as error: # FIXME clearer
            debug( "Google connection lost: %s" % error )
            try:
                message( "Google connection lost (%s %s), will re-connect" % (error['status'], error['reason']) )
            except Exception:
                message( "Google connection lost with unknown error, will re-connect: %s " % error )
            return (False,el) # el is empty here

        for an_event in feed.entry:
            where_string=''
            try:
                # join all 'where' entries together; you probably only have one anyway
                where_string=' // '.join(map(lambda w: w.value_string, an_event.where))
            except TypeError:
                # not all events have 'where' fields (value_string fields), and that's okay
                pass
            for a_when in an_event.when:
                for a_rem in a_when.reminder:
                    debug("event TEXT: %s METHOD: %s" % (an_event.title.text, a_rem.method) )
                    if a_rem.method == 'alert': # 'popup' in the web interface
                        # event (one for each alarm instance) is done,
                        # add it to the list
                        el.append(GcEvent(
                                    an_event.title.text,
                                    where_string,
                                    a_when.start_time,
                                    a_when.end_time,
                                    a_rem.minutes))
    return (True,el)

# ----------------------------
def do_login(calendarservice):
    """
    (Re)Login to Google Calendar.
    This sometimes fails or the connection dies, so do_login() needs to be done again.
    
    calendarservice: as returned by get_calendar_service()
    returns: True or False (logged-in or failed)
    
    """
    try:
        calendarservice.ProgrammaticLogin()
    except Exception as error:
        debug( 'Failed to authenticate to Google: %s' % error )
        message( 'Failed to authenticate to Google as %s' % calendarservice.email )
        message( 'Check username, password and that the account is enabled.' )
        return False
    message( "Logged in to Google Calendar as %s" % calendarservice.email )
    return True # we're logged in

# -------------------------------------------------------------------------------------------
def process_events_thread():
    """Process events and raise alarms via pynotify"""
    # initialize notification system
    if not pynotify.init('gcalert-Calendar_Alerter-%s' % myversion):
        print "Could not initialize pynotify / libnotify!"
        sys.exit(1)
    time.sleep(threads_offset) # give a chance for the other thread to get some events
    while 1:
        nowunixtime = time.time()
        debug("p_e_t: running")
        events_lock.acquire()
        for e in events:
            if e.starttime_unix < nowunixtime:
                debug("p_e_t: removing %s, is gone" % e)
                events.remove(e)
                # also free up some memory
                if e in alarmed_events:
                    alarmed_events.remove(e)
            # it starts in the future
            # check for alarm times if it wasn't alarmed yet
            elif e not in alarmed_events:
                # check alarm time. If it's now-ish, raise alarm
                # otherwise, let the event sleep some more
                # alarm now if the alarm has 'started'
                if nowunixtime >= e.alarm_time_unix:
                    e.alarm()
                    alarmed_events.append(e)
                else:
                    debug("p_e_t: not yet: \"%s\" (%s) [now:%d, alarm:%d]" % ( e.title, e.starttime_str, nowunixtime, e.alarm_time_unix ))
            else:
                debug("p_e_t: already alarmed: \"%s\" (%s) [now:%d, alarm:%d]" % ( e.title, e.starttime_str, nowunixtime, e.alarm_time_unix ))
        events_lock.release()
        debug("p_e_t: finished")
        # we can't just sleep until the next event as the other thread MIGHT
        # add something new meanwhile
        time.sleep(alarm_sleeptime)

def usage():
    """Print usage information."""
    print "gcalert version %s" % myversion
    print "Poll Google Calendar and display alarms on events that have alarms defined."
    print "Usage: gcalert.py [options]"
    print " -s F, --secret=F : specify location of a file containing"
    print "                    username and password, newline-separated"
    print "                    Default: $HOME/.gcalert_secret"
    print " -d, --debug      : produce debug messages"
    print " -q N, --query=N  : poll Google every N seconds for newly added events"
    print "                    (default: %d)" % query_sleeptime
    print " -a M, --alarm=M  : awake and produce alarms every N seconds(default: %d)" % alarm_sleeptime
    print " -l L, --look=L   : \"look ahead\" L days in the calendar for events"
    print "                    (default: %d)" % lookahead_days
    print " -r R, --retry=R  : sleep R seconds between reconnect attempts (default: %d)" % login_retry_sleeptime

def get_calendar_service():
    """
    Get hold of a CalendarService() and stick username/password info in it, plus some settings.
    Return the results if successful, exit the program if not.
    """
    # get credentials from file
    cs = gdata.calendar.service.CalendarService()
    try:
        # the 'password file' should contain two lines with username and password
        # the :2 is there to allow a newline at the end
        (cs.email, cs.password) = open(secrets_file).read().split('\n')[:2]
    except IOError as error:
        print error 
        sys.exit(1)
    except ValueError:
        print "Password file %s should contain two newline-separated lines: username (without gmail.com) and password." % secrets_file
        sys.exit(2)
    except Exception as error:
        print "Something unhandled went wrong reading your password file '%s', please report this as a bug." % secrets_file
        print error
        sys.exit(3)

    # Full-fledged SSL needs the SSL patch (to python-gdata_1.2.4-0ubuntu2 at least)
    # see http://groups.google.com/group/gdata-python-client-library-contributors/browse_thread/thread/48254170a6f6818a?pli=1
    #
    # if not present, the login will go over SSL
    # but the actual calendar will be retrieved over plain HTTP
    #
    # tcpdump if unsure ;)
    cs.ssl = True;
    cs.source = 'gcalert-Calendar_Alerter-%s' % myversion
    return cs

def update_events_thread():
    """Periodically sync the 'events' list to what's in Google Calendar"""
    connectionstatus = do_login(cs)
    while 1:
        if(not connectionstatus):
            time.sleep(login_retry_sleeptime)
            connectionstatus = do_login(cs)
        else:
            debug("u_e_t: running")
            # today
            range_start = time.strftime("%Y-%m-%d",time.localtime())
            # tommorrow, or later
            range_end=time.strftime("%Y-%m-%d",time.localtime(time.time()+lookahead_days*24*3600))
            (connectionstatus,newevents) = date_range_query(cs, range_start, range_end)
            events_lock.acquire()
            now = time.time()
            # remove stale events
            for n in events:
                if not (n in newevents):
                    debug('Event deleted or modified: %s' % n)
                    events.remove(n)
            # add new events to the list
            for n in newevents:
                debug('Is new event N really new? THIS: %s' % n)
                if not (n in events):
                    debug('Received event: %s' % n)
                    # does it start in the future?
                    if now < n.starttime_unix:
                        debug("-> future, adding")
                        events.append(n)
                    else:
                        debug("-> past already")
            events_lock.release()
            debug("u_e_t: finished")
            time.sleep(query_sleeptime)

if __name__ == '__main__':
    # -------------------------------------------------------------------------------------------
    # the main thread will start up, then launch the background 'alarmer' thread,
    # and proceed check the calendar every so often
    #

    try:
        opts, args = getopt.getopt(sys.argv[1:], "hds:q:a:l:r:", ["help", "debug", "secret=", "query=", "alarm=", "look=", "retry="])
    except getopt.GetoptError as err:
        # print help information and exit:
        print str(err) # will print something like "option -a not recognized"
        sys.exit(2)

    try:
        for o, a in opts:
            if o == "-d":
                debug_flag = True
            elif o in ("-h", "--help"):
                usage()
                sys.exit()
            elif o in ("-s", "--secret"):
                secrets_file = a
                debug("secrets_file set to %s" % secrets_file)
            elif o in ("-q", "--query"):
                query_sleeptime = int(a) # FIXME handle non-integers graciously
                debug("query_sleeptime set to %d" % query_sleeptime)
            elif o in ("-a", "--alarm"):
                alarm_sleeptime = int(a)
                debug("alarm_sleeptime set to %d" % alarm_sleeptime)
            elif o in ("-l", "--look"):
                lookahead_days = int(a)
                debug("lookahead_days set to %d" % lookahead_days)
            elif o in ("-r", "--retry"):
                login_retry_sleeptime = int(a)
                debug("login_retry_sleeptime set to %d" % login_retry_sleeptime)
            else:
                assert False, "unhandled option"
    except ValueError:
        print "Option %s requires an integer parameter; use '-h' for help." % o
        sys.exit(1)

    cs = get_calendar_service()

    # set up ^C handler
    signal.signal( signal.SIGINT, stopthismadness ) 

    # start up the event processing thread
    debug("Starting p_e_t")
    thread.start_new_thread(process_events_thread,())

    # starting up
    message("gcalert %s running..." % myversion)
    debug("SETTINGS: secrets_file: %s alarm_sleeptime: %d query_sleeptime: %d lookahead_days: %d login_retry_sleeptime: %d" % ( secrets_file, alarm_sleeptime, query_sleeptime, lookahead_days, login_retry_sleeptime ))
    
    update_events_thread()

