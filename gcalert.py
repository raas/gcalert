#!/usr/bin/python
# vim: ai expandtab

# This script will periodically check your Google Calendar and display an alarm
# when that's set for the event. It reads ALL your calendars automatically.
#
# Requires: python-notify python-gdata notification-daemon python-dateutil
# Also, recommended for SSL: /afs/cern.ch/user/a/ahorvath/public/deb/python-gdata_1.2.4-0ubuntu2ssl_all.deb FIXME
# Edit below for username/pass
#
# FIXME:
# - gracious handling of missing pynotify 
# - command-line options
# - config file for username/password
# - funky icon for libnotify alert:)
# - add 'Location' string from feed


from gdata.calendar.service import *
import gdata.service
import gdata.calendar
import getopt
import sys
import time
import urllib
import pynotify
import thread
# magical date parser and timezone handler
from dateutil.tz import *
from dateutil.parser import *

# ----------------------------
alarm_sleeptime=30 # seconds
calendar_sleeptime=180 # seconds
range_days=2 # look this many days in the future
# ----------------------------

events=[] # all events seen so far that are yet to start
events_lock=thread.allocate_lock() # hold to access events[]
alarmed_events=[] # events (occurences etc) already done, minus those in the past

# ----------------------------
# get the list of 'magic strings' used to identify each calendar
# returns: list(username) that each can be used in CalendarEventQuery()
#
def GetUserCalendars(cs):
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
def DateRangeQuery(cs, start_date='2007-01-01', end_date='2007-07-01'):
    el=[] # event occurence list
    for username in GetUserCalendars(cs):
        # FIXME error checking
        try:
            query = gdata.calendar.service.CalendarEventQuery(username, 'private', 'full')
        except gdata.service.RequestError:
            print "** Error connecting to Google, will retry later **"
            return el

        query.start_min = start_date
        query.start_max = end_date 
        feed = cs.CalendarQuery(query)
        for an_event in feed.entry:
            for a_when in an_event.when:
                for a_rem in a_when.reminder:
                    # it's a separate 'event' for each reminder
                    # start/end times are datetime.datetime() objects here
                    # created by dateutil.parser.parse()
                    el.append({'title':an_event.title.text, 
                               'start':parse(a_when.start_time),
                               'end':parse(a_when.end_time),
                               'minutes':a_rem.minutes})
    return el

# ----------------------------

# alarm one event
def do_alarm(event):
    starttime=event['start'].astimezone(tzlocal()).strftime('%Y-%m-%d %H:%M:%S')
    print " ***** ALARM ALARM ALARM %s %s ****  " % ( event['title'],starttime ) 
    # FIXME add an icon here
    a=pynotify.Notification( event['title'], "Starting: %s" % starttime )
    # let the alarm stay until it's closed by hand (acknowledged)
    a.set_timeout(0)
    if not a.show():
        print "Failed to send notification!"


# -------------------------------------------------------------------------------------------
# alarming on events is run as a background op
#
def process_events_thread():
    # initialize alarm system
    if not pynotify.init("Basics"):
        sys.exit(1)
    time.sleep(3) # offset :)
    while 1:
        nowunixtime=time.time()
        # throw away old events
        print "p_e_t: running at %s" % time.ctime()
        events_lock.acquire()
        for e in events:
            e_start_unixtime=int(e['start'].astimezone(tzlocal()).strftime('%s'))
            if e_start_unixtime<nowunixtime:
                print "p_e_t: removing %s, is gone" % e
                events.remove(e)
                # also free up some memory
                if e in alarmed_events:
                    alarmed_events.remove(e)
            # it starts in the future
            # check for alarm times if it wasn't alarmed yet
            elif e not in alarmed_events:
                # calculate alarm time. If it's now-ish, raise alarm
                # otherwise, let the event sleep some more
                alarm_at_unixtime=e_start_unixtime-60*int(e['minutes'])
                # alarm now if the alarm has 'started'
                if nowunixtime >= alarm_at_unixtime:
                    do_alarm(e)
                    alarmed_events.append(e)
                else:
                    print "p_e_t: not yet: \"%s\" (%s) [n:%d, a:%d]" % ( e['title'],e['start'],nowunixtime,alarm_at_unixtime )
        events_lock.release()
        print "p_e_t: finished at %s" % time.ctime()
        # we can't just sleep until the next event as the other thread MIGHT
        # add something new meanwhile
        time.sleep(alarm_sleeptime)

# ----------------------------
def do_login(cs):
    try:
        cs.ProgrammaticLogin()
    except gdata.service.Error: # seriously, yes
        print 'Failed to authenticate to google.'
        print 'Check username, password and that the account is enabled.'
        sys.exit(1)

# -------------------------------------------------------------------------------------------
# the main thread will check the calendar every so often
#

# login
cs = CalendarService()
cs.email = 'probabela98'
# this needs the SSL patch to python-gdata
# if not present, the login will go over SSL
# but the actual calendar will be retrieved over plain HTTP
# tcpdump if unsure ;)
cs.ssl = True;
cs.password = 'pbela123'
cs.source = 'raas-Calendar_Alerter-0.1'

thread.start_new_thread(process_events_thread,())
do_login(cs)

while 1:
    print "main thread: running at %s " % time.ctime()
    # today
    range_start=time.strftime("%Y-%m-%d",time.localtime())
    # tommorrow, or later
    range_end=time.strftime("%Y-%m-%d",time.localtime(time.time()+range_days*24*3600))
    newevents=DateRangeQuery(cs, range_start, range_end)
    events_lock.acquire()
    now=time.time()
    # add new events to the list
    for n in newevents:
        if not n in events:
            print 'Received event: %s' % n
            # does it start in the future?
            if now<int(n['start'].astimezone(tzlocal()).strftime('%s')):
                print "-> future, adding"
                events.append(n)
            else:
                print "-> past already"
    events_lock.release()
    print "main thread: finished at %s, sleeping %d secs " % ( time.ctime(), calendar_sleeptime )
    time.sleep(calendar_sleeptime)
