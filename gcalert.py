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
# FIXME:
# - funky icon for libnotify alert:)
# - add 'Location' string from feed
# - warn for unsecure permissions of the password/secret file
# - option for strftime in alarms
# - use some sort of proper logging with log levels etc
# - options for selecting which calendars to alert (currently: all of them)
# - snooze buttons
# - proper main() instead of global vars, make this a good library as well as a program
# - testing (as in, unit testing), after having a main()

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
    from gdata.calendar.service import *
    import gdata.service
    import gdata.calendar
    # libnotify handler
    import pynotify
    # magical date parser and timezone handler
    from dateutil.tz import *
    from dateutil.parser import *
except ImportError as e:
    print "Dependency was not found! %s" % e
    print "(Try: apt-get install python-notify python-gdata python-dateutil notification-daemon)"
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
# -------------------------------------------------------------------------------------------
# end of user-changeable stuff here
# -------------------------------------------------------------------------------------------

events=[] # all events seen so far that are yet to start
events_lock=thread.allocate_lock() # hold to access events[]
alarmed_events = [] # events (occurences etc) already done, minus those in the past
connected = False # google connection is disconnected

# print debug messages if -d was given
# ----------------------------
def message(s):
    print "%s gcalert.py: %s" % ( time.asctime(), s)
    sys.stdout.flush()

def debug(s):
    if (debug_flag):
        message("DEBUG: %s" % s)

# ----------------------------
# signal handlers are easier than wrapping the whole show
# into one giant try/except looking for KeyboardInterrupt
# besides we have two threads to shut down
def stopthismadness(signl, frme):
	message("shutting down on SIGINT")
	sys.exit(0)

# ----------------------------
# get the list of 'magic strings' used to identify each calendar
# returns: list(username) that each can be used in CalendarEventQuery()
#
def get_user_calendars(cs):
    feed = cs.GetAllCalendarsFeed()
    # in there is the full feed URL and we need the last part (=='username')
    return map(lambda x: urllib.unquote(x.id.text.split('/')[-1]), feed.entry) 

# ----------------------------
# get a list of events happening between the given dates
# in all calendars the user has
# return: list of events
#
# each event record has fields 'title', 'start', 'end', 'minutes' 
# each reminder occurence creates a new event
#
# returns (connectionstatus, eventlist)
def date_range_query(cs, start_date='2007-01-01', end_date='2007-07-01'):
    el = [] # event occurence list
    try:
        for username in get_user_calendars(cs):
            query = gdata.calendar.service.CalendarEventQuery(username, 'private', 'full')

            query.start_min = start_date
            query.start_max = end_date 
            feed = cs.CalendarQuery(query)
            for an_event in feed.entry:
                for a_when in an_event.when:
                    for a_rem in a_when.reminder:
                        debug("event TEXT: %s METHOD: %s" % (a_rem.text, a_rem.method) )
                        if a_rem.method == 'alert': # 'popup' in the web interface
                            # it's a separate 'event' for each reminder
                            # start/end times are datetime.datetime() objects here
                            # created by dateutil.parser.parse()
                            start=parse(a_when.start_time)
                            end=parse(a_when.end_time)
                            # Google sometimes does not supply timezones
                            # (for events that last more than a day and no time set, apparently)
                            # python can't compare two dates if only one has TZ info
                            if not start.tzname():
                                start=start.replace(tzinfo=tzlocal())
                            if not end.tzname():
                                end=end.replace(tzinfo=tzlocal())
                            # event (one for each alarm instance) is done,
                            # add it to the list
                            el.append({'title':an_event.title.text, 
                                       'start':start,
                                       'end':end,
                                       'minutes':a_rem.minutes})
    except Exception as error: # FIXME clearer
        message( "Google connection lost, will re-connect" )
        debug( "Google connection lost: %s" % error )
        return (False,el) # el is empty here

    return (True,el)

# ----------------------------

# alarm one event
def do_alarm(event):
    starttime=event['start'].astimezone(tzlocal()).strftime('%Y-%m-%d  %H:%M')
    message( " ***** ALARM ALARM ALARM %s %s ****  " % ( event['title'],starttime )  )
    # FIXME add an icon here
    a=pynotify.Notification( event['title'], "Starting: %s" % starttime )
    # let the alarm stay until it's closed by hand (acknowledged)
    a.set_timeout(0)
    if not a.show():
        message( "Failed to send alarm notification!" )

# ----------------------------
# try to log in, return logged-in-ness (true for success)
def do_login(cs):
    try:
        cs.ProgrammaticLogin()
    #except gdata.service.Error: # seriously, yes, "Error"
    except Exception as error:
        message( 'Failed to authenticate to Google as %s' % cs.email )
        debug( 'Failed to authenticate to Google: %s' % error )
        message( 'Check username, password and that the account is enabled.' )
        return False
    message( "Logged in to Google Calendar as %s" % cs.email )
    return True # we're logged in

# -------------------------------------------------------------------------------------------
# alarming on events is run as a background op
#
def process_events_thread():
    # initialize alarm system
    if not pynotify.init("Basics"):
        print "Could not initialize pynotify / libnotify!"
        sys.exit(1)
    time.sleep(3) # offset :) needed by pynotify somehow?
    while 1:
        nowunixtime = time.time()
        # throw away old events
        debug("p_e_t: running")
        events_lock.acquire()
        for e in events:
            e_start_unixtime = int(e['start'].astimezone(tzlocal()).strftime('%s'))
            if e_start_unixtime < nowunixtime:
                debug("p_e_t: removing %s, is gone" % e)
                events.remove(e)
                # also free up some memory
                if e in alarmed_events:
                    alarmed_events.remove(e)
            # it starts in the future
            # check for alarm times if it wasn't alarmed yet
            elif e not in alarmed_events:
                # calculate alarm time. If it's now-ish, raise alarm
                # otherwise, let the event sleep some more
                alarm_at_unixtime = e_start_unixtime-60*int(e['minutes'])
                # alarm now if the alarm has 'started'
                if nowunixtime >= alarm_at_unixtime:
                    do_alarm(e)
                    alarmed_events.append(e)
                else:
                    debug("p_e_t: not yet: \"%s\" (%s) [n:%d, a:%d]" % ( e['title'],e['start'],nowunixtime,alarm_at_unixtime ))
        events_lock.release()
        debug("p_e_t: finished")
        # we can't just sleep until the next event as the other thread MIGHT
        # add something new meanwhile
        time.sleep(alarm_sleeptime)

# ----------------------------
def usage():
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

# -------------------------------------------------------------------------------------------
# the main thread will start up, then launch the background 'alarmer' thread,
# and proceed check the calendar every so often
#

try:
    opts, args = getopt.getopt(sys.argv[1:], "hds:q:a:l:r:", ["help", "debug", "secret=", "query=", "alarm=", "look=", "retry="])
except getopt.GetoptError, err:
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

# get credentials from file
cs = CalendarService()
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

thread.start_new_thread(process_events_thread,())
connectionstatus = do_login(cs)

# set up ^C handler
signal.signal( signal.SIGINT, stopthismadness ) 

# starting up
message("gcalert %s running..." % myversion)
debug("SETTINGS: secrets_file: %s alarm_sleeptime: %d query_sleeptime: %d lookahead_days: %d login_retry_sleeptime: %d" % ( secrets_file, alarm_sleeptime, query_sleeptime, lookahead_days, login_retry_sleeptime ))

while 1:
    if(not connectionstatus):
        time.sleep(login_retry_sleeptime)
        connectionstatus = do_login(cs)
    else:
        debug("main thread: running")
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
                if now < int(n['start'].astimezone(tzlocal()).strftime('%s')):
                    debug("-> future, adding")
                    events.append(n)
                else:
                    debug("-> past already")
        events_lock.release()
        debug("main thread: finished")
        time.sleep(query_sleeptime)
